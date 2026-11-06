#!/usr/bin/env python3
"""Content-addressed capture of agent transcripts.

Captured bytes are immutable objects named by digest. A generation records
what one capture of one native source observed: the chunk objects in order,
their offsets, and the source identity. Rewrites and truncations create a new
generation instead of destroying the previous one, and an unchanged source
reads no content bytes.
"""
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from agent_memory.types import DATABASE_HARNESSES, HARNESSES

CHUNK_BYTES = 65536
PROTOCOL = 1
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACTS = 200
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}")
SESSION_METADATA_FIELDS = ("cwd", "parent", "source", "thread_source", "ancestry", "branch_tip", "workspaces", "step_count")


class ArchiveError(RuntimeError):
    pass


class ArchiveForgotten(ArchiveError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_identity(db, label=None, regenerate=False):
    row = db.execute("SELECT id, origin_host, display_label FROM collector_identity WHERE singleton = 1").fetchone()
    if row is not None and not regenerate:
        return row["origin_host"], row["display_label"]
    origin = secrets.token_hex(16)
    display = str(label or "collector")
    now = time.time()
    verb = "INSERT OR REPLACE" if regenerate else "INSERT OR IGNORE"
    with db:
        db.execute(verb + " INTO collector_identity (singleton, id, origin_host, display_label, created_at) "
                   "VALUES (1, ?, ?, ?, ?)", (secrets.token_hex(16), origin, display, now))
        row = db.execute("SELECT origin_host, display_label FROM collector_identity WHERE singleton = 1").fetchone()
        db.execute("INSERT OR IGNORE INTO origin_host (id, display_label, created_at) VALUES (?, ?, ?)",
                   (row["origin_host"], row["display_label"], now))
    return row["origin_host"], row["display_label"]


def origin_for(db):
    row = db.execute("SELECT origin_host FROM collector_identity WHERE singleton = 1").fetchone()
    if row is None:
        return ensure_identity(db)[0]
    return row["origin_host"]


def object_path(archive_dir, digest):
    return Path(archive_dir) / "objects" / digest[:2] / digest


@contextlib.contextmanager
def object_lock(archive_dir, exclusive=False):
    """Serialize object removal against everything that reads or publishes objects.

    Capture, receipt, and backup hold the shared lock while they depend on an
    object file; only the unlink of an unreferenced object takes it exclusively.
    """
    path = Path(archive_dir) / ".objects.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(descriptor)


def _write_object(archive_dir, data):
    digest = sha256(data)
    target = object_path(archive_dir, digest)
    if target.is_file() and not target.is_symlink() and target.stat().st_size == len(data):
        if sha256(target.read_bytes()) == digest:
            return digest, len(data), False
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = target.with_name(".tmp." + secrets.token_hex(8))
    try:
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(staging, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            staging.unlink()
        except OSError:
            pass
        raise
    return digest, len(data), True


def read_object(archive_dir, digest):
    path = object_path(archive_dir, digest)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read()
    if sha256(data) != digest:
        raise ArchiveError("archived object is corrupt: " + digest)
    return data


def generation_chunks(db, generation):
    return [
        (int(row["offset"]), int(row["size"]), row["digest"])
        for row in db.execute(
            "SELECT offset, size, digest FROM archive_generation_object WHERE generation = ? ORDER BY ordinal",
            (int(generation),),
        )
    ]


def read_generation(db, archive_dir, generation, offset=0, length=None):
    chunks = generation_chunks(db, generation)
    if not chunks:
        raise ArchiveError("generation has no captured objects")
    wanted_from = int(offset)
    wanted_to = None if length is None else wanted_from + int(length)
    parts = []
    for start, size, chunk_digest in chunks:
        end = start + size
        if end <= wanted_from or (wanted_to is not None and start >= wanted_to):
            continue
        data = read_object(archive_dir, chunk_digest)
        lo = max(0, wanted_from - start)
        hi = len(data) if wanted_to is None else min(len(data), wanted_to - start)
        parts.append(data[lo:hi])
    return b"".join(parts)


def read_generation_lines(db, archive_dir, generation, state, limit=8 * 1024 * 1024):
    """Yield (offset, line, length) for each complete line, buffering one line at a time.

    A line longer than the limit is yielded as None so it can be reported
    without being held in memory.

    The final partial line, if any, is reported through state["partial"] instead
    of being yielded, so an incomplete tail is never indexed as a record.
    """
    buffer = bytearray()
    length = 0
    offset = 0
    state["partial"] = 0
    for _start, _size, chunk_digest in generation_chunks(db, generation):
        data = read_object(archive_dir, chunk_digest)
        position = 0
        while True:
            newline = data.find(b"\n", position)
            end = len(data) if newline < 0 else newline + 1
            length += end - position
            if length <= limit:
                buffer.extend(data[position:end])
            else:
                buffer.clear()
            if newline < 0:
                break
            yield offset, (bytes(buffer) if length <= limit else None), length
            offset += length
            buffer.clear()
            length = 0
            position = end
    state["partial"] = length


def read_line_at(db, archive_dir, generation, position, limit=8 * 1024 * 1024):
    """Read one newline-terminated record starting at position, across chunks."""
    chunks = generation_chunks(db, generation)
    if not chunks:
        raise ArchiveError("generation has no captured objects")
    position = int(position)
    collected = bytearray()
    for start, size, chunk_digest in chunks:
        end = start + size
        if end <= position:
            continue
        data = read_object(archive_dir, chunk_digest)
        lo = max(0, position - start)
        collected.extend(data[lo:])
        if b"\n" in collected:
            break
        if len(collected) > limit:
            raise ArchiveError("archived record exceeds the read limit")
    line, separator, _ = bytes(collected).partition(b"\n")
    if not separator:
        raise ArchiveError("archived record is not newline terminated")
    return line + b"\n"


def verify_generation(db, archive_dir, generation):
    chunks = generation_chunks(db, generation)
    for _start, size, chunk_digest in chunks:
        data = read_object(archive_dir, chunk_digest)
        if len(data) != size:
            raise ArchiveError("archived object size mismatch: " + chunk_digest)
    return len(chunks)


def last_generation(db, origin_host, harness, native_id):
    return db.execute(
        "SELECT * FROM archive_generation WHERE origin_host = ? AND harness = ? AND native_id = ? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (origin_host, harness, native_id),
    ).fetchone()


def _same_source(generation, info):
    if generation is None:
        return False
    return (
        int(generation["source_size"]) == info.st_size
        and int(generation["source_dev"]) == info.st_dev
        and int(generation["source_ino"]) == info.st_ino
        and int(generation["source_mtime_ns"]) == info.st_mtime_ns
        and int(generation["source_ctime_ns"]) == info.st_ctime_ns
    )


def normalize_session_metadata(value):
    """Validate native session metadata and return it in its canonical form."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(SESSION_METADATA_FIELDS):
        raise ArchiveError("session metadata must carry exactly: " + ", ".join(SESSION_METADATA_FIELDS))
    for field in ("cwd",):
        if not isinstance(value[field], str):
            raise ArchiveError("session metadata field is not text: " + field)
    for field in ("parent", "source", "thread_source", "branch_tip", "workspaces"):
        if value[field] is not None and not isinstance(value[field], str):
            raise ArchiveError("session metadata field is not text: " + field)
    if value["ancestry"] not in ("known", "unknown"):
        raise ArchiveError("session metadata ancestry must be known or unknown")
    steps = value["step_count"]
    if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 0):
        raise ArchiveError("session metadata step_count must be a non-negative integer")
    canonical = {field: value[field] for field in SESSION_METADATA_FIELDS}
    if len(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)) > MAX_METADATA_BYTES:
        raise ArchiveError("session metadata exceeds its size limit")
    return canonical


def metadata_json(metadata):
    if metadata is None:
        return None
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def source_digest_of(chunk_digests):
    digest = hashlib.sha256()
    for chunk_digest in chunk_digests:
        digest.update(chunk_digest.encode("ascii"))
    return digest.hexdigest()


def file_digests(path):
    """Return the generation source digest, source size, and whole-file digest in one pass."""
    full = hashlib.sha256()
    sequence = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            data = stream.read(CHUNK_BYTES)
            if not data:
                break
            full.update(data)
            sequence.update(sha256(data).encode("ascii"))
            size += len(data)
    return sequence.hexdigest(), size, full.hexdigest()


def legacy_copies(db, archive_dir):
    """Yield (path, size, redundant) for the pre-object-store transcript trees.

    A copy is redundant when its captured bytes are already a generation or an
    artifact. Files under a database harness stay in place because that archive
    is still the only captured form.
    """
    for root in sorted(Path(archive_dir).iterdir()):
        if not root.is_dir() or root.name not in HARNESSES or root.name in DATABASE_HARNESSES:
            continue
        for directory, _dirnames, filenames in os.walk(root, followlinks=False):
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if path.is_symlink() or not path.is_file():
                    continue
                source_digest, size, digest = file_digests(path)
                redundant = db.execute(
                    "SELECT 1 FROM archive_generation WHERE harness = ? AND source_digest = ? "
                    "AND source_size = ? LIMIT 1", (root.name, source_digest, size)).fetchone() is not None
                if not redundant:
                    redundant = db.execute(
                        "SELECT 1 FROM archive_artifact WHERE digest = ? LIMIT 1", (digest,)).fetchone() is not None
                yield path, size, redundant


def manifest_digest(source_digest, source_size, artifacts, metadata):
    """Name one immutable generation: its transcript, companions, and native metadata."""
    body = {
        "source_digest": source_digest,
        "source_size": int(source_size),
        "artifacts": sorted([str(name), digest] for name, digest in artifacts),
        "metadata": metadata,
    }
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _require_digest(value, what):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ArchiveError("%s is not a sha256 digest" % what)
    return value


def _require_size(value, what, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise ArchiveError("%s is out of bounds" % what)
    return value


@dataclasses.dataclass(frozen=True)
class Manifest:
    origin_host: str
    harness: str
    native_id: str
    source_path: str
    source_digest: str
    source_size: int
    captured_at: float
    chunks: tuple[tuple[int, int, str], ...]
    artifacts: tuple[tuple[str, str, int], ...]
    session_metadata: dict[str, Any] | None
    manifest_digest: str
    sizes: dict[str, int]


def validate_manifest(manifest: dict[str, Any]) -> Manifest:
    """Check a received generation manifest end to end and return its normalized form.

    The chunk list must tile the declared source exactly, and the declared
    identity digests must be the ones the content implies.
    """
    for field in ("origin_host", "harness", "native_id", "source_path"):
        if not isinstance(manifest.get(field), str) or (field != "source_path" and not manifest[field]):
            raise ArchiveError("manifest field is missing or not text: " + field)
    if manifest["harness"] not in HARNESSES:
        raise ArchiveError("manifest names an unsupported harness: " + manifest["harness"])
    source_size = _require_size(manifest.get("source_size"), "source size", 1 << 50)
    captured_at = manifest.get("captured_at")
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
        raise ArchiveError("manifest capture time is not a number")
    chunks, artifacts = manifest.get("chunks"), manifest.get("artifacts")
    if not isinstance(chunks, list) or not isinstance(artifacts, list):
        raise ArchiveError("manifest chunks and artifacts must be lists")
    if len(artifacts) > MAX_ARTIFACTS:
        raise ArchiveError("manifest carries too many artifacts")
    sizes = {}
    offset = 0
    clean_chunks: list[tuple[int, int, str]] = []
    for item in chunks:
        if not isinstance(item, dict):
            raise ArchiveError("manifest chunk is not an object")
        digest = _require_digest(item.get("digest"), "chunk digest")
        size = _require_size(item.get("size"), "chunk size", MAX_OBJECT_BYTES)
        if size == 0:
            raise ArchiveError("manifest chunk is empty")
        if item.get("offset") != offset:
            raise ArchiveError("manifest chunks are not contiguous from offset zero")
        if sizes.setdefault(digest, size) != size:
            raise ArchiveError("conflicting sizes for one digest")
        clean_chunks.append((offset, size, digest))
        offset += size
    if offset != source_size:
        raise ArchiveError("manifest chunks total %d bytes but the source declares %d" % (offset, source_size))
    source_digest = _require_digest(manifest.get("source_digest"), "source digest")
    if source_digest_of(digest for _offset, _size, digest in clean_chunks) != source_digest:
        raise ArchiveError("manifest source digest does not match its chunks")
    names = set()
    clean_artifacts: list[tuple[str, str, int]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise ArchiveError("manifest artifact is not an object")
        name = item.get("name")
        if not isinstance(name, str) or not name or "/" in name or "\0" in name or name in (".", "..") or name in names:
            raise ArchiveError("manifest artifact name is invalid or repeated")
        names.add(name)
        digest = _require_digest(item.get("digest"), "artifact digest")
        size = _require_size(item.get("size"), "artifact size", MAX_ARTIFACT_BYTES)
        if sizes.setdefault(digest, size) != size:
            raise ArchiveError("conflicting sizes for one digest")
        clean_artifacts.append((name, digest, size))
    metadata = normalize_session_metadata(manifest.get("session_metadata"))
    claimed = _require_digest(manifest.get("manifest_digest"), "manifest digest")
    if manifest_digest(source_digest, source_size, [(name, digest) for name, digest, _size in clean_artifacts], metadata) != claimed:
        raise ArchiveError("manifest digest does not match its content")
    return Manifest(
        origin_host=manifest["origin_host"], harness=manifest["harness"], native_id=manifest["native_id"],
        source_path=manifest["source_path"], source_digest=source_digest, source_size=source_size,
        captured_at=float(captured_at), chunks=tuple(clean_chunks), artifacts=tuple(clean_artifacts),
        session_metadata=metadata, manifest_digest=claimed, sizes=sizes)


def artifact_digests(db, generation):
    return {row["name"]: row["digest"] for row in db.execute(
        "SELECT name, digest FROM archive_artifact WHERE generation = ?", (int(generation),))}


def generation_manifest(db, generation) -> dict[str, Any]:
    """Describe one stored generation in the form validate_manifest and the sender use."""
    row = db.execute("SELECT * FROM archive_generation WHERE id = ?", (int(generation),)).fetchone()
    if row is None:
        raise ArchiveError("generation does not exist: " + str(generation))
    return {
        "origin_host": row["origin_host"], "harness": row["harness"], "native_id": row["native_id"],
        "source_path": row["source_path"], "source_digest": row["source_digest"], "source_size": int(row["source_size"]),
        "captured_at": float(row["captured_at"]),
        "chunks": [{"digest": digest, "size": size, "offset": offset} for offset, size, digest in generation_chunks(db, generation)],
        "artifacts": [
            {"name": item["name"], "digest": item["digest"], "size": int(item["size"])}
            for item in db.execute("SELECT name, digest, size FROM archive_artifact WHERE generation = ? ORDER BY name", (int(generation),))],
        "session_metadata": json.loads(row["session_metadata"]) if row["session_metadata"] else None,
        "manifest_digest": row["manifest_digest"],
    }


def _register_objects(db, written):
    """Catalog newly written objects at once so a failed capture leaves nothing a purge cannot find."""
    if not written:
        return
    now = time.time()
    with db:
        db.executemany("INSERT OR IGNORE INTO archive_object (digest, size, created_at) VALUES (?, ?, ?)",
                       [(digest, size, now) for digest, size in written])


def _write_artifacts(db, archive_dir, artifacts):
    stored = []
    written = []
    try:
        for name, data in artifacts:
            artifact_digest, size, wrote = _write_object(archive_dir, data)
            if wrote:
                written.append((artifact_digest, size))
            stored.append((str(name), artifact_digest, size))
    finally:
        _register_objects(db, written)
    return stored


def publish_generation(db, *, origin_host, harness, native_id, source_path, source_digest, source_size, identity, metadata,
             workspace, captured, chunks, artifacts, stat_fields, from_generation=None):
    """Insert one complete generation atomically; return (generation id, created)."""
    now = time.time()
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM tombstone WHERE origin_host = ? AND harness = ? AND native_id = ?",
                      (origin_host, harness, native_id)).fetchone() is not None:
            raise ArchiveForgotten("session was forgotten: %s %s" % (harness, native_id))
        latest = last_generation(db, origin_host, harness, native_id)
        if latest is not None and latest["manifest_digest"] == identity:
            db.rollback()
            return int(latest["id"]), False
        cursor = db.execute(
            "INSERT INTO archive_generation (origin_host, harness, native_id, source_path, source_digest, source_size, "
            "captured_at, protocol, stored_at, source_dev, source_ino, source_mtime_ns, source_ctime_ns, workspace_path, "
            "manifest_digest, session_metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (origin_host, harness, native_id, str(source_path), source_digest, int(source_size), captured, PROTOCOL, now,
             *stat_fields, workspace, identity, metadata_json(metadata)))
        generation = int(cursor.lastrowid or 0)
        if from_generation is not None:
            copied = db.execute(
                "INSERT INTO archive_generation_object (generation, ordinal, digest, size, offset) "
                "SELECT ?, ordinal, digest, size, offset FROM archive_generation_object WHERE generation = ?",
                (generation, int(from_generation))).rowcount
            if copied == 0 and int(source_size) > 0:
                raise ArchiveError("the previous generation disappeared during capture; retry capture")
        for index, (offset, size, chunk_digest) in enumerate(chunks):
            db.execute("INSERT OR IGNORE INTO archive_object (digest, size, created_at) VALUES (?, ?, ?)", (chunk_digest, size, now))
            db.execute(
                "INSERT INTO archive_generation_object (generation, ordinal, digest, size, offset) VALUES (?, ?, ?, ?, ?)",
                (generation, index, chunk_digest, size, offset))
        for name, artifact_digest, size in artifacts:
            db.execute("INSERT OR IGNORE INTO archive_object (digest, size, created_at) VALUES (?, ?, ?)", (artifact_digest, size, now))
            db.execute("INSERT INTO archive_artifact (generation, name, digest, size) VALUES (?, ?, ?, ?)",
                       (generation, name, artifact_digest, size))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return generation, True


def capture(db, archive_dir, harness, native_id, source_path, *, artifacts=(), session_metadata=None, workspace=None, stats=None):
    """Capture the current bytes of source_path as a verified, immutable generation.

    A generation names its transcript, companion artifacts, and native session
    metadata together. Any change to one of them creates a new generation; the
    previous one is never edited. Returns (generation_id, created). Raises
    OSError when the source cannot be read as a regular file.
    """
    stats = stats if stats is not None else {}
    archive_dir = Path(archive_dir)
    metadata = normalize_session_metadata(session_metadata)
    for name, data in artifacts:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArchiveError("artifact exceeds its size limit: " + str(name))
    if len(artifacts) > MAX_ARTIFACTS:
        raise ArchiveError("too many artifacts")
    artifact_digests = [(str(name), sha256(data)) for name, data in artifacts]
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source_path, flags)
    with os.fdopen(descriptor, "rb") as stream, object_lock(archive_dir):
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("refusing to capture a non-regular file: " + str(source_path))
        origin_host = origin_for(db)
        if db.execute("SELECT 1 FROM tombstone WHERE origin_host = ? AND harness = ? AND native_id = ?",
                      (origin_host, harness, native_id)).fetchone() is not None:
            raise ArchiveForgotten("session was forgotten: %s %s" % (harness, native_id))
        previous = last_generation(db, origin_host, harness, native_id)
        cwd = metadata["cwd"] if metadata is not None else workspace
        if _same_source(previous, info):
            identity = manifest_digest(previous["source_digest"], previous["source_size"], artifact_digests, metadata)
            if previous["manifest_digest"] == identity:
                stats["archive_unchanged"] = stats.get("archive_unchanged", 0) + 1
                return int(previous["id"]), False
            generation, created = publish_generation(
                db, origin_host=origin_host, harness=harness, native_id=native_id, source_path=source_path,
                source_digest=previous["source_digest"], source_size=previous["source_size"], identity=identity,
                metadata=metadata, workspace=cwd or None, captured=time.time(), chunks=[],
                artifacts=_write_artifacts(db, archive_dir, artifacts),
                stat_fields=(previous["source_dev"], previous["source_ino"], previous["source_mtime_ns"], previous["source_ctime_ns"]),
                from_generation=previous["id"])
            key = "archived" if created else "archive_unchanged"
            stats[key] = stats.get(key, 0) + 1
            return generation, created
        previous_chunks = generation_chunks(db, previous["id"]) if previous is not None else []
        previous_digests = {digest for _offset, _size, digest in previous_chunks}
        stream.seek(0)
        all_chunks = []
        written = []
        start = 0
        try:
            while True:
                data = stream.read(CHUNK_BYTES)
                if not data:
                    break
                stats["bytes_read"] = stats.get("bytes_read", 0) + len(data)
                chunk_digest = sha256(data)
                if chunk_digest not in previous_digests:
                    chunk_digest, size, wrote = _write_object(archive_dir, data)
                    if wrote:
                        written.append((chunk_digest, size))
                        stats["archive_objects"] = stats.get("archive_objects", 0) + 1
                        stats["archive_bytes"] = stats.get("archive_bytes", 0) + size
                all_chunks.append((start, len(data), chunk_digest))
                start += len(data)
        finally:
            _register_objects(db, written)
        final = os.fstat(stream.fileno())
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
            raise ArchiveError("source changed during capture; retry capture")
        source_digest = source_digest_of(chunk_digest for _offset, _size, chunk_digest in all_chunks)
        identity = manifest_digest(source_digest, final.st_size, artifact_digests, metadata)
        generation, created = publish_generation(
            db, origin_host=origin_host, harness=harness, native_id=native_id, source_path=source_path,
            source_digest=source_digest, source_size=final.st_size, identity=identity, metadata=metadata,
            workspace=cwd or None, captured=time.time(), chunks=all_chunks,
            artifacts=_write_artifacts(db, archive_dir, artifacts),
            stat_fields=(final.st_dev, final.st_ino, final.st_mtime_ns, final.st_ctime_ns))
    key = "archived" if created else "archive_unchanged"
    stats[key] = stats.get(key, 0) + 1
    return generation, created


def capture_bytes(db, archive_dir, harness, native_id, source_path, data, *, session_metadata=None,
                  workspace=None, stat_fields=(0, 0, 0, 0), stats=None):
    """Capture an in-memory source as an immutable generation."""
    stats = stats if stats is not None else {}
    archive_dir = Path(archive_dir)
    metadata = normalize_session_metadata(session_metadata)
    chunks = []
    written = []
    try:
        with object_lock(archive_dir):
            for start in range(0, len(data), CHUNK_BYTES):
                chunk = data[start:start + CHUNK_BYTES]
                digest, size, wrote = _write_object(archive_dir, chunk)
                if wrote:
                    written.append((digest, size))
                    stats["archive_objects"] = stats.get("archive_objects", 0) + 1
                    stats["archive_bytes"] = stats.get("archive_bytes", 0) + size
                chunks.append((start, len(chunk), digest))
            _register_objects(db, written)
        source_digest = source_digest_of(chunk_digest for _offset, _size, chunk_digest in chunks)
        identity = manifest_digest(source_digest, len(data), [], metadata)
        generation, created = publish_generation(
            db, origin_host=origin_for(db), harness=harness, native_id=native_id, source_path=source_path,
            source_digest=source_digest, source_size=len(data), identity=identity, metadata=metadata,
            workspace=workspace, captured=time.time(), chunks=chunks, artifacts=[], stat_fields=stat_fields)
    finally:
        _register_objects(db, written)
    key = "archived" if created else "archive_unchanged"
    stats[key] = stats.get(key, 0) + 1
    return generation, created


def capture_artifacts(paths, limit=MAX_ARTIFACTS):
    """Read bounded, session-owned artifact files without following symlinks.

    Returns (artifacts, refused). A companion that exists but is not a regular
    file is refused and counted instead of being followed.
    """
    collected = []
    refused = 0
    for name, path in paths:
        if len(collected) >= limit:
            break
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            refused += 1
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            refused += 1
            continue
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            collected.append((str(name), stream.read(MAX_ARTIFACT_BYTES + 1)))
    return collected, refused


def retention_report(db):
    return {
        "generations": db.execute("SELECT count(*) FROM archive_generation").fetchone()[0],
        "objects": db.execute("SELECT count(*) FROM archive_object").fetchone()[0],
        "bytes": db.execute("SELECT coalesce(sum(size), 0) FROM archive_object").fetchone()[0],
        "tombstoned": db.execute("SELECT count(*) FROM archive_generation WHERE tombstoned = 1").fetchone()[0],
    }


def pin_manifest(db, destination):
    generations = [dict(row) for row in db.execute(
        "SELECT id, origin_host, harness, native_id, source_digest, source_size, captured_at FROM archive_generation ORDER BY id")]
    objects = [dict(row) for row in db.execute("SELECT digest, size FROM archive_object ORDER BY digest")]
    manifest = {"protocol": PROTOCOL, "generations": generations, "objects": objects}
    Path(destination).write_text(json.dumps(manifest, sort_keys=True, indent=1))
    return manifest
