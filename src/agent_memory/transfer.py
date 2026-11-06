#!/usr/bin/env python3
"""Versioned transfer of captured generations over a byte stream.

The same receiver serves the in-process local transport and an SSH pipe, so a
quoting or framing bug cannot hide behind an untested local path. Every frame
is length-prefixed; no transcript content, path, or identifier is ever
interpolated into a shell command.
"""
import hashlib
import json
import socket
import struct
import subprocess
import threading
import time
import zlib
from typing import Any, NoReturn

from agent_memory import archive as archive_module
from agent_memory import learning_sync
from agent_memory.learning import apply_remote_forget

PROTOCOL = 2
MAX_FRAME = 4 * 1024 * 1024
MAX_BLOB = 64 * 1024 * 1024
COMPRESSION = "zlib"
LOCAL_SOCKET_TIMEOUT = 900
IO_SLICE = 65536


def compress(data):
    return zlib.compress(data, 6)


def decompress(data, expected):
    """Inflate to at most the declared size; a larger object is refused."""
    if expected < 0 or expected > MAX_BLOB:
        raise TransferError("declared object size is out of bounds")
    engine = zlib.decompressobj()
    try:
        out = engine.decompress(data, expected + 1)
        if len(out) > expected or engine.unconsumed_tail:
            raise TransferError("compressed object exceeds its declared size")
        out += engine.flush()
    except zlib.error as exc:
        raise TransferError("object failed to decompress: " + str(exc)) from exc
    if len(out) != expected:
        raise TransferError("decompressed object does not match its declared size")
    return out


class TransferError(RuntimeError):
    pass


def _read_exactly(stream, size):
    chunks = []
    remaining = size
    while remaining:
        data = stream.read(min(remaining, IO_SLICE))
        if not data:
            raise TransferError("connection closed mid-frame")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def write_message(stream, value):
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise TransferError("frame exceeds the protocol limit")
    stream.write(struct.pack(">Q", len(payload)))
    stream.write(payload)
    stream.flush()


def read_message(stream):
    header = _read_exactly(stream, 8)
    size = struct.unpack(">Q", header)[0]
    if size > MAX_FRAME:
        raise TransferError("frame exceeds the protocol limit")
    payload = _read_exactly(stream, size)
    try:
        value = json.loads(payload)
    except ValueError as exc:
        raise TransferError("frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise TransferError("frame is not an object")
    return value


def write_blob(stream, data):
    if len(data) > MAX_BLOB:
        raise TransferError("object exceeds the protocol limit")
    stream.write(struct.pack(">Q", len(data)))
    view = memoryview(data)
    for start in range(0, len(view), IO_SLICE):
        stream.write(view[start:start + IO_SLICE])
    stream.flush()


def read_blob(stream, size=None):
    declared = struct.unpack(">Q", _read_exactly(stream, 8))[0]
    if declared > MAX_BLOB:
        raise TransferError("object exceeds the protocol limit")
    if size is not None and declared != size:
        raise TransferError("object length does not match the manifest")
    return _read_exactly(stream, declared)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class Sender:
    def __init__(self, store, remote, instream, outstream, origin, label):
        self.store, self.remote = store, remote
        self.instream, self.outstream = instream, outstream
        self.origin, self.label = origin, label
        self.compression = None
        self.endpoint = str(remote)
        self.raw_bytes = 0
        self.sent_bytes = 0
        self.stats: dict[str, Any] = {"delivered": [], "raw_bytes": 0, "sent_bytes": 0,
                                      "compression": None, "endpoint": str(remote)}

    def _expect(self, kind):
        message = read_message(self.instream)
        if message.get("type") == "error":
            raise TransferError(str(message.get("message")))
        if message.get("type") != kind:
            raise TransferError("expected %s frame, received %s" % (kind, message.get("type")))
        return message

    def handshake(self):
        write_message(self.outstream, {"type": "hello", "protocol": PROTOCOL, "origin": self.origin,
                                       "label": self.label, "compression": [COMPRESSION]})
        ready = self._expect("ready")
        if ready.get("protocol") != PROTOCOL:
            raise TransferError("central store speaks protocol %r" % ready.get("protocol"))
        self.compression = ready.get("compression")
        store_origin = ready.get("store")
        if isinstance(store_origin, str) and store_origin:
            self.endpoint = store_origin

    def send_tombstones(self):
        rows = self.store.db.execute(
            "SELECT origin_host, harness, native_id, scope, forgotten_at FROM tombstone WHERE origin_host = ?",
            (self.origin,)).fetchall()
        for row in rows:
            payload = {"type": "tombstone", "origin_host": row["origin_host"], "harness": row["harness"],
                       "native_id": row["native_id"], "scope": row["scope"], "forgotten_at": row["forgotten_at"]}
            write_message(self.outstream, payload)
            self._expect("tombstone_ack")
            self.store.mark_tombstone_durable(row["origin_host"], row["harness"], row["native_id"], self.remote)

    def record_attempt(self, generation, remote, error):
        self.store.record_attempt(generation, remote, error)

    def pending(self):
        return self.store.db.execute(
            "SELECT g.* FROM archive_generation g LEFT JOIN transfer_generation t "
            "ON t.generation = g.id AND t.remote = ? WHERE t.durable_at IS NULL ORDER BY g.id",
            (self.endpoint,)).fetchall()

    def send_generation(self, generation):
        if str(generation["origin_host"]) != self.origin:
            raise TransferError("refusing to send a generation from another origin")
        manifest: dict[str, Any] = {"type": "generation", **archive_module.generation_manifest(self.store.db, int(generation["id"]))}
        write_message(self.outstream, manifest)
        needed = self._expect("need")
        wanted = set(needed.get("digests") or [])
        for item in [*manifest["chunks"], *manifest["artifacts"]]:
            if item["digest"] not in wanted:
                continue
            self._send_object(item["digest"])
            wanted.discard(item["digest"])
        if wanted:
            raise TransferError("central store requested an unknown object")
        write_message(self.outstream, {"type": "commit"})
        ack = self._expect("ack")
        if ack.get("origin_host") != manifest["origin_host"] or ack.get("manifest_digest") != manifest["manifest_digest"]:
            raise TransferError("acknowledgement does not match the delivered generation")
        return int(ack["generation"])

    def send_learning(self):
        """Deliver this host's source-backed learning records once per change."""
        db = self.store.db
        if not learning_sync.has_learning(db):
            return False
        learning_sync.ensure_schema(db)
        records = learning_sync.export_snapshot(db)
        digest = learning_sync.snapshot_digest(records)
        previous = learning_sync.durable_digest(db, self.endpoint)
        if previous == digest or (previous is None and not records):
            return False
        write_message(self.outstream, {"type": "learning_begin", "count": len(records), "digest": digest})
        for record in records:
            write_message(self.outstream, {"type": "learning_record", "record": record})
        write_message(self.outstream, {"type": "learning_commit"})
        ack = self._expect("learning_ack")
        if ack.get("digest") != digest:
            raise TransferError("acknowledgement does not match the delivered learning snapshot")
        learning_sync.mark_durable(db, self.endpoint, digest)
        self.stats["learning_records"] = len(records)
        return True

    def _send_object(self, digest):
        data = self.store.read_object_bytes(digest)
        encoding = "identity"
        payload = data
        if self.compression == COMPRESSION:
            packed = compress(data)
            if len(packed) < len(data):
                encoding, payload = COMPRESSION, packed
        self.raw_bytes += len(data)
        self.sent_bytes += len(payload)
        write_message(self.outstream, {"type": "object", "digest": digest, "encoding": encoding})
        write_blob(self.outstream, payload)

    def run(self, attempts=5, progress=None):
        try:
            self.handshake()
            self.send_tombstones()
        except (OSError, TransferError) as exc:
            for generation in self.pending():
                self.record_attempt(generation["id"], self.endpoint, str(exc))
            raise TransferError("cannot reach the central store: %s" % exc) from exc
        delivered = []
        for generation in self.pending():
            last = None
            for attempt in range(attempts):
                try:
                    local_id = self.send_generation(generation)
                    self.store.mark_durable(generation["id"], self.endpoint, local_id)
                    delivered.append(int(generation["id"]))
                    if progress is not None:
                        progress(len(delivered))
                    break
                except (OSError, TransferError) as exc:
                    last = exc
                    self.record_attempt(generation["id"], self.endpoint, str(exc))
                    if attempt + 1 >= attempts:
                        raise TransferError("delivery failed after %d attempts: %s" % (attempts, last)) from last
                    time.sleep(min(60, 2 ** attempt))
        try:
            self.send_learning()
        except learning_sync.SnapshotError as exc:
            raise TransferError("learning snapshot is not exportable: %s" % exc) from exc
        write_message(self.outstream, {"type": "bye"})
        self._expect("done")
        self.stats.update({"delivered": delivered, "raw_bytes": self.raw_bytes, "sent_bytes": self.sent_bytes,
                           "compression": self.compression, "endpoint": self.endpoint})
        return delivered


def receiver(store, instream, outstream, allowed_origins=None):
    """Serve one connection. Bound to a fixed command; content is never code."""
    hello = read_message(instream)
    if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL:
        write_message(outstream, {"type": "error", "message": "unsupported protocol"})
        return
    origin = str(hello.get("origin") or "")
    if allowed_origins is not None and origin not in allowed_origins:
        write_message(outstream, {"type": "error", "message": "origin is not allowed: " + origin})
        return
    if not origin:
        write_message(outstream, {"type": "error", "message": "origin is required"})
        return
    offered = hello.get("compression")
    compression = COMPRESSION if isinstance(offered, list) and COMPRESSION in offered else None
    write_message(outstream, {"type": "ready", "protocol": PROTOCOL, "compression": compression,
                              "store": archive_module.origin_for(store.db)})
    while True:
        try:
            message = read_message(instream)
        except TransferError:
            raise
        kind = message.get("type")
        if kind == "bye":
            write_message(outstream, {"type": "done"})
            return
        if kind == "tombstone":
            if message.get("origin_host") != origin:
                _refuse(outstream, "tombstone origin does not match the connection")
            apply_remote_forget(store, origin, str(message["harness"]), str(message["native_id"]),
                                str(message["scope"]), float(message["forgotten_at"]))
            write_message(outstream, {"type": "tombstone_ack"})
            continue
        if kind == "generation":
            _receive_generation(store, instream, outstream, message, origin)
            continue
        if kind == "learning_begin":
            _receive_learning(store, instream, outstream, message, origin)
            continue
        if kind == "query":
            try:
                rows = handle_query(store, str(message.get("op")), message.get("args") or {})
            except (KeyError, ValueError, TypeError, OSError) as exc:
                write_message(outstream, {"type": "error", "message": str(exc)})
                continue
            write_message(outstream, {"type": "result", "rows": rows})
            continue
        write_message(outstream, {"type": "error", "message": "unsupported frame: " + str(kind)})
        return


def _refuse(outstream, message) -> NoReturn:
    write_message(outstream, {"type": "error", "message": message})
    raise TransferError(message)


def _receive_learning(store, instream, outstream, begin, origin):
    count, digest = begin.get("count"), begin.get("digest")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= learning_sync.MAX_RECORDS or not isinstance(digest, str):
        _refuse(outstream, "malformed learning snapshot header")
    records = []
    for _ in range(count):
        message = read_message(instream)
        if message.get("type") != "learning_record":
            _refuse(outstream, "expected a learning record")
        records.append(message.get("record"))
    if read_message(instream).get("type") != "learning_commit":
        _refuse(outstream, "expected the learning snapshot commit")
    try:
        accepted = learning_sync.import_snapshot(store, origin, records, digest)
    except learning_sync.SnapshotError as exc:
        _refuse(outstream, str(exc))
    write_message(outstream, {"type": "learning_ack", "digest": digest, "count": count, "accepted": accepted})


def _receive_generation(store, instream, outstream, manifest, connection_origin):
    try:
        clean = archive_module.validate_manifest(manifest)
    except archive_module.ArchiveError as exc:
        _refuse(outstream, str(exc))
    origin, harness, native = clean.origin_host, clean.harness, clean.native_id
    if origin != connection_origin:
        _refuse(outstream, "generation origin does not match the connection")
    if store.is_tombstoned(origin, harness, native):
        _refuse(outstream, "session is tombstoned")
    acknowledgement = {"type": "ack", "origin_host": origin, "manifest_digest": clean.manifest_digest, "durable": True}
    existing = store.latest_generation_id(origin, harness, native, clean.manifest_digest)
    if existing is not None and store.generation_complete(existing) and store.generation_indexed(existing):
        write_message(outstream, {"type": "need", "digests": []})
        _expect_object_or_commit(instream, outstream, store, [])
        write_message(outstream, {**acknowledgement, "generation": int(existing)})
        return
    wanted = [digest for digest, size in clean.sizes.items() if not store.object_intact(digest, size)]
    write_message(outstream, {"type": "need", "digests": wanted})
    received = {}
    while True:
        message = read_message(instream)
        if message.get("type") == "commit":
            break
        if message.get("type") != "object":
            _refuse(outstream, "expected an object or commit")
        digest = str(message.get("digest") or "")
        expected = clean.sizes.get(digest)
        if expected is None or digest not in wanted:
            _refuse(outstream, "object was not requested")
        encoding = message.get("encoding") or "identity"
        try:
            payload = read_blob(instream)
            if encoding == COMPRESSION:
                data = decompress(payload, expected)
            elif encoding == "identity":
                data = payload
            else:
                raise TransferError("unsupported object encoding: " + str(encoding))
            if len(data) != expected or _digest(data) != digest:
                raise TransferError("object failed verification: " + digest)
        except TransferError as exc:
            _refuse(outstream, str(exc))
        received[digest] = data
    try:
        generation = store.commit_generation(manifest, received)
    except RuntimeError as exc:
        _refuse(outstream, "generation was not committed: %s" % exc)
    write_message(outstream, {**acknowledgement, "generation": int(generation)})


def _expect_object_or_commit(instream, outstream, store, wanted):
    while True:
        message = read_message(instream)
        if message.get("type") == "commit":
            return
        if message.get("type") != "object":
            write_message(outstream, {"type": "error", "message": "unexpected frame"})
            raise TransferError("unexpected frame")
        data = read_blob(instream)
        store.store_object(data)


class _ActivityStream:
    """Record activity so the transport timeout measures idleness, not duration."""

    def __init__(self, stream, activity):
        self._stream = stream
        self._activity = activity

    def read(self, size=-1):
        data = self._stream.read(size)
        self._activity["t"] = time.monotonic()
        return data

    def write(self, data):
        written = self._stream.write(data)
        self._activity["t"] = time.monotonic()
        return written

    def flush(self):
        self._stream.flush()
        self._activity["t"] = time.monotonic()

    def close(self):
        self._stream.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def ssh_argv(host, command):
    return ["ssh", "-o", "BatchMode=yes", host, "--", command]


def _drain(stream, sink, limit=65536):
    try:
        while True:
            data = stream.read(4096)
            if not data:
                return
            if len(sink[0]) < limit:
                sink[0] += data
    except (OSError, ValueError):
        return


class SshTransport:
    """One SSH child process whose silence is bounded by a real inactivity deadline.

    A watchdog kills the process when nothing has been read or written for
    `timeout` seconds, which unblocks any pending read. Leaving the context
    reaps the process and fails loudly if it cannot be reaped.
    """

    def __init__(self, host, command, timeout=120):
        self.host, self.command, self.timeout = host, command, timeout
        self.timed_out = False
        self._activity = {"t": time.monotonic()}
        self._finished = threading.Event()
        self._stderr = [b""]

    def __enter__(self):
        self.process = subprocess.Popen(ssh_argv(self.host, self.command), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdin, stdout, stderr = self.process.stdin, self.process.stdout, self.process.stderr
        assert stdin is not None and stdout is not None and stderr is not None
        self._pipes = (stdin, stdout, stderr)
        self.instream = _ActivityStream(stdout, self._activity)
        self.outstream = _ActivityStream(stdin, self._activity)
        self._drainer = threading.Thread(target=_drain, args=(stderr, self._stderr), daemon=True)
        self._drainer.start()
        self._watchdog = threading.Thread(target=self._expire, daemon=True)
        self._watchdog.start()
        return self

    def _expire(self):
        while not self._finished.wait(min(2.0, self.timeout / 4)):
            if time.monotonic() - self._activity["t"] > self.timeout:
                self.timed_out = True
                self.process.kill()
                return

    def failure(self, error):
        if self.timed_out:
            reason = "ssh transport timed out after %s seconds without activity" % self.timeout
        else:
            reason = "ssh transport failed: %s" % error
        detail = self._stderr[0].decode("utf-8", "replace").strip()[-600:]
        return TransferError(reason + (("; remote stderr: " + detail) if detail else ""))

    def __exit__(self, exc_type, exc_value, traceback):
        self._finished.set()
        if exc_type is not None:
            self.process.kill()
        try:
            self._pipes[0].close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            if exc_type is None:
                raise TransferError("ssh transport did not exit after the stream closed") from None
        finally:
            for stream in self._pipes[1:]:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            self._watchdog.join(timeout=5)
            self._drainer.join(timeout=5)
        if self.process.poll() is None:
            raise TransferError("ssh transport process could not be reaped")


def ssh_sender(store, remote, host, command, origin, label, timeout=120, progress=None) -> dict[str, Any]:
    transport = SshTransport(host, command, timeout)
    with transport:
        try:
            sender = Sender(store, remote, transport.instream, transport.outstream, origin, label)
            sender.run(progress=progress)
            return sender.stats
        except (OSError, ValueError, TransferError) as exc:
            raise transport.failure(exc) from exc


def local_sync(source_store, central_state_dir, remote, origin, label, attempts=3, progress=None) -> dict[str, Any]:
    """Run the sender and receiver over a real socketpair in one process.

    The receiver opens its own store: a SQLite connection belongs to the thread
    that created it, and reusing the caller's handle here is a real defect.
    """
    import threading

    from agent_memory.store import Store

    left, right = socket.socketpair()
    left.settimeout(LOCAL_SOCKET_TIMEOUT)
    right.settimeout(LOCAL_SOCKET_TIMEOUT)
    failure = {}

    def serve():
        try:
            with Store(central_state_dir) as central:
                with right.makefile("rb") as instream, right.makefile("wb") as outstream:
                    receiver(central, instream, outstream, allowed_origins=[origin])
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            failure["error"] = exc
            try:
                right.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with left.makefile("rb") as instream, left.makefile("wb") as outstream:
            sender = Sender(source_store, remote, instream, outstream, origin, label)
            sender.run(attempts, progress=progress)
            stats = sender.stats
    finally:
        try:
            left.close()
        except OSError:
            pass
        thread.join(timeout=LOCAL_SOCKET_TIMEOUT)
    if "error" in failure:
        raise TransferError("central receiver failed: " + str(failure["error"])) from failure["error"]
    return stats


def handle_query(store, op, args):
    if op == "memory":
        limit = int(args.get("limit") or 20)
        tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "learning_experiences" not in tables:
            return learning_sync.imported_rows(store.db, limit)
        rows = []
        for row in store.db.execute(
                "SELECT id, project, kind, claim, applies_when, observed_at, state FROM learning_experiences "
                "WHERE state = 'retained' ORDER BY observed_at DESC LIMIT ?", (limit,)):
            sources = [dict(item) for item in store.db.execute(
                "SELECT session, position, digest, role FROM learning_experience_sources WHERE experience = ?",
                (row["id"],))]
            rows.append({
                "record": "experience:%d" % row["id"],
                "project": row["project"],
                "kind": row["kind"],
                "claim": row["claim"],
                "applies_when": row["applies_when"],
                "observed_at": row["observed_at"],
                "sources": sources,
                "authority": learning_sync.AUTHORITY,
            })
        return sorted(rows + learning_sync.imported_rows(store.db, limit), key=lambda item: item["observed_at"], reverse=True)[:limit]
    if op == "search":
        rows = store.search(args["query"], args.get("project"), limit=int(args.get("limit") or 20),
                            regex=bool(args.get("regex")), kinds=args.get("kinds"), since=args.get("since"))
    elif op == "recent":
        rows = store.recent(args.get("project"), limit=int(args.get("limit") or 20),
                            kinds=args.get("kinds"), since=args.get("since"))
    elif op == "show":
        rows = store.session_events(args["session"], args.get("project"), position=args.get("position"),
                                    before=int(args.get("before", 3)), after=int(args.get("after", 3)))
    else:
        raise TransferError("unsupported query: " + str(op))
    verified = []
    for row in rows:
        record = dict(row)
        record["evidence"] = store.resolve_evidence(row["session"], row["position"], row["digest"])
        verified.append(record)
    return verified


def query(store, op, args):
    return handle_query(store, op, args)


def query_over_ssh(host, command, op, args, origin, label, timeout=120):
    transport = SshTransport(host, command, timeout)
    with transport:
        try:
            write_message(transport.outstream, {"type": "hello", "protocol": PROTOCOL, "origin": origin, "label": label})
            ready = read_message(transport.instream)
            if ready.get("type") != "ready":
                raise TransferError(str(ready.get("message") or "central store refused the query"))
            write_message(transport.outstream, {"type": "query", "op": op, "args": args})
            reply = read_message(transport.instream)
            write_message(transport.outstream, {"type": "bye"})
            read_message(transport.instream)
        except (OSError, ValueError, TransferError) as exc:
            raise transport.failure(exc) from exc
    if reply.get("type") == "error":
        raise TransferError(str(reply.get("message")))
    if reply.get("type") != "result":
        raise TransferError("unexpected query reply")
    return reply.get("rows") or []
