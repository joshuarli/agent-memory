#!/usr/bin/env python3
"""Persistent local stack and end-to-end lifecycle and security tests."""
import base64
import hashlib
import http.client
import json
import os
import select
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
WORK_TMP = ROOT / ".work" / "tmp"
WORKER = ROOT / "cloudflare"
WORK_TMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402
sys.path.insert(0, str(testkit.SRC))

from agent_memory import archive as archive_module  # noqa: E402
from agent_memory import experience as experience_module  # noqa: E402
from agent_memory import learning as learning_module  # noqa: E402
from agent_memory import mirror  # noqa: E402
from agent_memory import mirror_crypto  # noqa: E402
from agent_memory.store import Store  # noqa: E402

DEV_ROOT = ROOT / ".work" / "local" / "dev"
PORT = 8790


def ensure_age():
    try:
        mirror_crypto.binary()
        return
    except mirror_crypto.CryptoError:
        result = subprocess.run(
            ["sh", str(WORKER / "dev-age.sh")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise AssertionError("cannot build rage: " + result.stderr.strip()) from None
        os.environ["AGE_BIN"] = result.stdout.strip()


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_until(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class LocalStack:
    def __init__(self, origins, extra=()):
        node = shutil.which("node")
        if node is None:
            raise AssertionError("node is required for the local stack")
        self.process = subprocess.Popen(
            [node, "local/cli.mjs", "serve", "--ephemeral",
             "--origins", json.dumps(origins), "--test-state", *extra],
            cwd=str(WORKER), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout = self.process.stdout
        if stdout is None:
            raise AssertionError("local stack has no output stream")
        ready, _, _ = select.select([stdout], [], [], 30)
        if not ready:
            self.stop()
            raise AssertionError("local stack did not start")
        line = stdout.readline()
        try:
            self.info = json.loads(line)
        except ValueError:
            self.stop()
            raise AssertionError("local stack did not start: " + (line.strip() or "timeout")) from None
        self.port = self.info["port"]
        self.url = self.info.get("url") or "https://127.0.0.1:%d" % self.port
        self.tls = ssl.create_default_context(cafile=self.info["ca_file"])

    def stop(self):
        process, self.process = getattr(self, "process", None), None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        for path in WORK_TMP.glob("stack-%d-*" % process.pid):
            shutil.rmtree(path, ignore_errors=True)

    def request(self, method, path, body=None, raw=None, headers=None):
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        merged = dict(headers or {})
        if body is not None:
            merged.setdefault("content-type", "application/json")
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=120, context=self.tls)
        try:
            connection.request(method, path, body=payload, headers=merged)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def d1(self, sql, params=None, mode="all"):
        status, _headers, body = self.request(
            "POST", "/__test/d1", {"sql": sql, "params": params or [], "mode": mode})
        if status != 200:
            raise AssertionError("d1 control failed: " + body.decode("utf-8", "replace"))
        value = json.loads(body)
        if mode == "first":
            return value["row"]
        if mode == "run":
            return value["meta"]
        return value["results"]

    def r2_keys(self, prefix=None):
        path = "/__test/r2" + ("" if prefix is None else "?prefix=" + quote(prefix, safe=""))
        status, _headers, body = self.request("GET", path)
        if status != 200:
            raise AssertionError("r2 list failed: " + body.decode("utf-8", "replace"))
        return json.loads(body)["keys"]

    def r2_get(self, key):
        return self.request("GET", "/__test/r2?key=" + quote(key, safe=""))

    def r2_put(self, key, data):
        return self.request("PUT", "/__test/r2?key=" + quote(key, safe=""), raw=data)

    def r2_delete(self, key):
        return self.request("DELETE", "/__test/r2?key=" + quote(key, safe=""))

    def tail(self):
        status, _headers, body = self.request("GET", "/__test/tail")
        if status != 200:
            raise AssertionError("tail control failed: " + body.decode("utf-8", "replace"))
        return json.loads(body)["events"]


class StackTestCase(unittest.TestCase):
    ORIGINS = [{"client_id": "collector-client.access", "origin": "origin-a"}]

    @classmethod
    def setUpClass(cls):
        ensure_age()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = self.root / "age.key"
        self.recipient = mirror_crypto.keygen(self.identity)
        self.recipients_file = self.root / "recipients.txt"
        self.recipients_file.write_text(self.recipient + "\n")
        self.recipients = mirror_crypto.load_recipients(self.recipients_file)
        self.start_stack()
        self.configure_stack()

    def start_stack(self, extra=()):
        self.stack = LocalStack(self.ORIGINS, extra)
        self.addCleanup(self.stack.stop)

    def configure_stack(self):
        self.token_file = Path(self.stack.info["token_files"]["origin-a"])
        self.config = mirror.MirrorConfig(
            url=self.stack.url,
            service_token_file=self.token_file,
            age_recipients_file=self.recipients_file,
            age_identity_file=self.identity,
            learning_projection=False,
        )

    def restart(self, extra):
        self.stack.stop()
        self.start_stack(extra)
        self.configure_stack()

    def token_headers(self, origin="origin-a"):
        value = json.loads(Path(self.stack.info["token_files"][origin]).read_text())
        return {
            "CF-Access-Client-Id": value["client_id"],
            "CF-Access-Client-Secret": value["client_secret"],
        }

    def store(self, name="state", origin="origin-a"):
        store = Store(self.root / name)
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO collector_identity(singleton, id, origin_host, display_label, created_at) "
                "VALUES(1, ?, ?, 'synthetic', 0)",
                (origin, origin))
        self.addCleanup(store.close)
        return store

    def capture(self, store, native, text):
        source = self.root / (native + ".jsonl")
        source.write_text(json.dumps({
            "type": "message", "id": "m-" + native, "parentId": None,
            "timestamp": "2026-10-30T00:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}],
                        "meta": {"messageId": "m-" + native}},
        }, separators=(",", ":")) + "\n")
        return archive_module.capture(store.db, store.archive_dir, "codex", native, source)

    def client(self, config=None, sleep=time.sleep):
        return mirror.MirrorClient(
            config or self.config,
            factory=lambda: http.client.HTTPSConnection(
                "127.0.0.1", self.stack.port, timeout=30, context=self.stack.tls),
            sleep=sleep)

    def sync(self, store, config=None, sleep=time.sleep):
        client = self.client(config, sleep)
        try:
            return mirror.sync(
                store, config or self.config, "cloudflare", client=client,
                upload_client_factory=lambda: self.client(config, sleep),
                recipients=self.recipients, concurrency=1)
        finally:
            client.close()

    def snapshots(self, config=None):
        client = self.client(config)
        try:
            return mirror.snapshots(config or self.config, client=client)
        finally:
            client.close()

    def verify(self, config=None, objects=True):
        client = self.client(config)
        try:
            return mirror.verify(config or self.config, client=client, objects=objects)
        finally:
            client.close()

    def gc(self, config=None, apply=False):
        client = self.client(config)
        try:
            return mirror.gc(config or self.config, client=client, apply=apply)
        finally:
            client.close()


class MirrorLifecycleTests(StackTestCase):
    def test_a_fresh_sync_stores_ciphertext_objects_and_a_snapshot_page(self):
        store = self.store()
        self.capture(store, "native-1", "fresh sync plaintext marker")
        result = self.sync(store)
        self.assertEqual(result["generations_committed"], 1)
        self.assertEqual(result["snapshots_created"], 1)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation WHERE origin_host = ?", ["origin-a"], "first")["count"], 1)
        objects = self.stack.d1(
            "SELECT plaintext_digest, ciphertext_digest, plaintext_size FROM remote_object "
            "WHERE origin_host = ? ORDER BY plaintext_digest", ["origin-a"])
        self.assertGreaterEqual(len(objects), 2)
        keys = {item["key"] for item in self.stack.r2_keys("v1/o/")}
        for row in objects:
            self.assertIn("v1/o/" + row["ciphertext_digest"], keys)
            status, _headers, body = self.stack.r2_get("v1/o/" + row["ciphertext_digest"])
            self.assertEqual(status, 200)
            self.assertEqual(hashlib.sha256(body).hexdigest(), row["ciphertext_digest"])
            self.assertNotIn(b"fresh sync plaintext marker", body)
        snapshot = self.stack.d1("SELECT id, objects_digest FROM snapshot", mode="first")
        page_key = "v1/s/origin-a/%s/0" % snapshot["id"]
        status, _headers, page = self.stack.r2_get(page_key)
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(page).hexdigest(), snapshot["objects_digest"])

    def test_an_unchanged_sync_writes_nothing(self):
        store = self.store()
        self.capture(store, "native-1", "unchanged sync marker")
        self.sync(store)
        before = {
            "generation_object": self.stack.d1("SELECT count(*) AS count FROM generation_object", mode="first")["count"],
            "remote_object": self.stack.d1("SELECT count(*) AS count FROM remote_object", mode="first")["count"],
            "revision": self.stack.d1(
                "SELECT revision FROM origin_sync WHERE origin_host = ?", ["origin-a"], "first")["revision"],
            "keys": {item["key"] for item in self.stack.r2_keys()},
        }
        second = self.sync(store)
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["generations_committed"], 0)
        self.assertEqual(second["objects_stored"], 0)
        self.assertEqual(second["snapshots_created"], 0)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation_object", mode="first")["count"],
            before["generation_object"])
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM remote_object", mode="first")["count"], before["remote_object"])
        self.assertEqual(self.stack.d1(
            "SELECT revision FROM origin_sync WHERE origin_host = ?", ["origin-a"], "first")["revision"],
            before["revision"])
        self.assertEqual({item["key"] for item in self.stack.r2_keys()}, before["keys"])

    def test_a_modified_session_creates_a_new_generation(self):
        store = self.store()
        self.capture(store, "native-1", "first version")
        self.sync(store)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation WHERE native_id = 'native-1'", mode="first")["count"], 1)
        self.capture(store, "native-1", "second version with more bytes")
        result = self.sync(store)
        self.assertEqual(result["generations_committed"], 1)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation WHERE native_id = 'native-1'", mode="first")["count"], 2)
        self.assertGreaterEqual(self.stack.d1(
            "SELECT revision FROM origin_sync WHERE origin_host = ?", ["origin-a"], "first")["revision"], 2)

    def test_snapshot_verification_attests_and_gc_keeps_it(self):
        store = self.store()
        self.capture(store, "native-1", "snapshot verification marker")
        self.sync(store)
        snapshot = self.snapshots()[0]
        self.assertEqual(self.stack.d1(
            "SELECT verified_at FROM snapshot WHERE id = ?", [snapshot["id"]], "first")["verified_at"], None)
        verified = self.verify(objects=True)
        self.assertEqual(verified["attested"], 1)
        self.assertIsNotNone(self.stack.d1(
            "SELECT verified_at FROM snapshot WHERE id = ?", [snapshot["id"]], "first")["verified_at"])
        retained = self.gc(apply=False)
        self.assertEqual(retained["snapshots_retained"], 1)
        self.assertEqual(retained["snapshots_pruned"], 0)

    def test_a_tombstone_and_gc_sweep_an_unpinned_object(self):
        store = self.store()
        self.capture(store, "native-1", "pinned snapshot marker")
        self.sync(store)
        self.capture(store, "native-2", "unpinned sweep marker")
        self.sync(store)
        native_two = self.stack.d1(
            "SELECT go.plaintext_digest, ro.ciphertext_digest FROM generation_object go "
            "JOIN generation g ON g.id = go.generation JOIN remote_object ro "
            "ON ro.origin_host = g.origin_host AND ro.plaintext_digest = go.plaintext_digest "
            "WHERE g.native_id = 'native-2'", mode="all")
        self.assertGreater(len(native_two), 0)
        store.forget_session("codex:native-2", time.time())
        self.sync(store)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM tombstone WHERE native_id = 'native-2'", mode="first")["count"], 1)
        for row in native_two:
            self.stack.d1(
                "UPDATE remote_object SET gc_pending_at = ? WHERE plaintext_digest = ?",
                [time.time() - 3601, row["plaintext_digest"]], "run")
        swept = self.gc(apply=True)
        self.assertGreaterEqual(swept["objects_swept"], 1)
        for row in native_two:
            self.assertEqual(self.stack.r2_get("v1/o/" + row["ciphertext_digest"])[0], 404)
            self.assertIsNone(self.stack.d1(
                "SELECT plaintext_digest FROM remote_object WHERE plaintext_digest = ?",
                [row["plaintext_digest"]], "first"))

    def test_a_snapshot_restores_a_generation_and_projected_learning(self):
        store = self.store()
        self.capture(store, "native-1", "restore marker with a searchable claim")
        learning_module.initialize(store.db)
        draft = experience_module.Draft(
            kind="method", claim="Restored recall finds the synthetic projection claim.",
            applies_when="Running the restored recall test.", detector="synthetic",
            subject="restore", observed_at=100.0,
            sources=[{"session": "codex:native-1", "position": 0,
                      "digest": "a" * 64, "role": "trigger"}])
        with store.db:
            status, _ = experience_module.add_experience(
                store.db, "git:" + str(self.root), draft, "codex:native-1", 100.0)
        self.assertIn(status, ("created", "merged"))
        projected = replace(self.config, learning_projection=True)
        self.sync(store, projected)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM learning_experience", mode="first")["count"], 1)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM memory_fts WHERE memory_fts MATCH ?",
            ["projection"], "first")["count"], 1)
        snapshot = self.snapshots()[0]["id"]
        destination = self.root / "restored"
        client = self.client()
        try:
            report = mirror.restore(
                self.config, "cloudflare", snapshot, destination,
                client=client, identity_path=self.identity)
        finally:
            client.close()
        self.assertEqual(report["replayed"], 0)
        restored = Store(destination)
        try:
            self.assertEqual(restored.db.execute(
                "SELECT count(*) FROM archive_generation").fetchone()[0], 1)
            self.assertEqual(restored.db.execute(
                "SELECT count(*) FROM learning_experiences").fetchone()[0], 1)
            self.assertEqual(restored.db.execute(
                "SELECT count(*) FROM learning_recall_fts WHERE learning_recall_fts MATCH ?",
                ("projection",)).fetchone()[0], 1)
        finally:
            restored.close()


class SecurityTests(StackTestCase):
    ORIGINS = [
        {"client_id": "collector-client.access", "origin": "origin-a"},
        {"client_id": "other-client.access", "origin": "origin-b"},
    ]

    def test_every_route_refuses_an_anonymous_request(self):
        digest = "a" * 64
        paths = [
            "/api/v1/status", "/api/v1/sync/hello", "/api/v1/sync/objects/missing",
            "/api/v1/sync/objects/" + digest, "/api/v1/sync/generations",
            "/api/v1/sync/manifests/" + digest, "/api/v1/sync/snapshots",
            "/api/v1/sync/snapshots/" + digest, "/api/v1/sync/snapshots/" + digest + "/objects",
            "/api/v1/sync/gc", "/api/v1/sync/tombstones", "/api/v1/sync/learning/state",
            "/api/v1/sync/learning/deltas", "/api/v1/sync/learning/keys",
            "/api/v1/sync/learning/reconcile", "/api/v1/memories",
            "/api/v1/memories/origin-a/experience/1", "/mcp", "/", "/search",
            "/m/origin-a/experience/1", "/status",
        ]
        for path in paths:
            status, _headers, _body = self.stack.request("GET", path)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.stack.d1("SELECT count(*) AS count FROM origin_sync", mode="first")["count"], 0)

    def test_a_forged_access_assertion_is_refused_without_state(self):
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
        claims = {
            "type": "app", "aud": ["local-audience"], "exp": int(time.time()) + 600,
            "iat": int(time.time()) - 10, "iss": "https://team.example.test", "sub": "",
            "common_name": "collector-client.access",
        }
        forged = encode({"alg": "RS256", "kid": "local-key", "typ": "JWT"}) + "." + encode(claims) + ".Zm9yZ2Vk"
        status, _headers, body = self.stack.request(
            "GET", "/api/v1/status", headers={"Cf-Access-Jwt-Assertion": forged})
        self.assertEqual(status, 403, body)
        missing, _headers, _body = self.stack.request("GET", "/api/v1/status")
        self.assertEqual(missing, 403)
        self.assertEqual(self.stack.d1("SELECT count(*) AS count FROM origin_sync", mode="first")["count"], 0)

    def test_reader_and_collector_permissions_are_separate(self):
        reader_status, _headers, _body = self.stack.request(
            "GET", "/api/v1/status", headers={"x-bench-principal": "reader"})
        self.assertEqual(reader_status, 200)
        reader_write, _headers, _body = self.stack.request(
            "POST", "/api/v1/sync/hello", {"protocol": 1, "origin": "origin-a"},
            headers={"x-bench-principal": "reader"})
        self.assertEqual(reader_write, 403)
        collector_status, _headers, _body = self.stack.request(
            "GET", "/api/v1/status", headers=self.token_headers())
        self.assertEqual(collector_status, 200)
        collector_page, _headers, _body = self.stack.request("GET", "/", headers=self.token_headers())
        self.assertEqual(collector_page, 403)

    def test_a_collector_cannot_use_another_origin(self):
        status, _headers, body = self.stack.request(
            "POST", "/api/v1/sync/hello", {"protocol": 1, "origin": "origin-b"},
            headers=self.token_headers("origin-a"))
        self.assertEqual(status, 403, body)
        self.assertEqual(self.stack.d1("SELECT count(*) AS count FROM origin_sync", mode="first")["count"], 0)

    def test_a_collector_cannot_read_another_origin_object(self):
        store = self.store()
        self.capture(store, "native-1", "cross origin object marker")
        self.sync(store)
        row = self.stack.d1(
            "SELECT ciphertext_digest FROM remote_object WHERE origin_host = 'origin-a' LIMIT 1", mode="first")
        status, _headers, _body = self.stack.request(
            "GET", "/api/v1/sync/objects/" + row["ciphertext_digest"], headers=self.token_headers("origin-b"))
        self.assertEqual(status, 404)

    def test_no_secret_or_plaintext_reaches_responses_or_the_tail(self):
        store = self.store()
        marker = "sensitive plaintext marker should never leave the client"
        self.capture(store, "native-1", marker)
        result = self.sync(store)
        token = json.loads(Path(self.stack.info["token_files"]["origin-a"]).read_text())
        identity = self.identity.read_text()
        status_body = self.stack.request("GET", "/api/v1/status", headers=self.token_headers())[2]
        memories_body = self.stack.request("GET", "/api/v1/memories", headers=self.token_headers())[2]
        tail = b""
        deadline = time.time() + 2
        events = []
        while time.time() < deadline:
            events = self.stack.tail()
            if events:
                break
            time.sleep(0.1)
        tail = json.dumps(events).encode()
        haystacks = [json.dumps(result).encode(), status_body, memories_body, tail]
        for haystack in haystacks:
            self.assertNotIn(token["client_secret"].encode(), haystack)
            self.assertNotIn(identity.encode(), haystack)
            self.assertNotIn(b"AGE-SECRET-KEY", haystack)
            self.assertNotIn(marker.encode(), haystack)
            self.assertNotIn(b"Cf-Access-Jwt-Assertion", haystack)


class ReliabilityTests(StackTestCase):
    @staticmethod
    def no_sleep(_delay):
        pass

    def drops(self):
        status, _headers, body = self.stack.request("GET", "/__test/faults")
        self.assertEqual(status, 200)
        return json.loads(body)["drops"]

    def test_an_interrupted_object_upload_retries(self):
        self.restart(("--drop-request-path", "/api/v1/sync/objects/", "--drop-request-method", "PUT"))
        store = self.store()
        self.capture(store, "native-1", "interrupted upload marker")
        result = self.sync(store, sleep=self.no_sleep)
        self.assertEqual(result["generations_committed"], 1)
        self.assertEqual(self.stack.d1("SELECT count(*) AS count FROM generation", mode="first")["count"], 1)
        self.assertGreaterEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation_object", mode="first")["count"], 1)
        self.assertEqual(self.drops(), 1)

    def test_a_lost_commit_response_reacks_without_extra_rows(self):
        self.restart(("--drop-response-path", "/api/v1/sync/generations", "--drop-response-method", "POST"))
        store = self.store()
        self.capture(store, "native-1", "lost commit response marker")
        result = self.sync(store, sleep=self.no_sleep)
        self.assertEqual(result["generations_committed"], 1)
        self.assertEqual(self.stack.d1("SELECT count(*) AS count FROM generation", mode="first")["count"], 1)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation_object", mode="first")["count"], 1)
        self.assertEqual(self.drops(), 1)

    def test_a_failed_d1_batch_retries_cleanly(self):
        self.restart(("--d1-batch-fault-every", "1"))
        store = self.store()
        self.capture(store, "native-1", "failed batch marker")
        result = self.sync(store, sleep=self.no_sleep)
        self.assertEqual(result["generations_committed"], 1)
        self.assertEqual(self.stack.d1("SELECT count(*) AS count FROM generation", mode="first")["count"], 1)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation_object", mode="first")["count"], 1)

    def test_a_missing_r2_object_is_detected(self):
        store = self.store()
        self.capture(store, "native-1", "missing object marker")
        self.sync(store)
        row = self.stack.d1(
            "SELECT ro.ciphertext_digest FROM generation_object go JOIN generation g ON g.id = go.generation "
            "JOIN remote_object ro ON ro.origin_host = g.origin_host AND ro.plaintext_digest = go.plaintext_digest "
            "WHERE g.native_id = 'native-1' ORDER BY go.ordinal LIMIT 1", mode="first")
        status, _headers, _body = self.stack.r2_delete("v1/o/" + row["ciphertext_digest"])
        self.assertEqual(status, 200)
        with self.assertRaises(mirror.MirrorError):
            self.verify(objects=True)

    def test_a_corrupted_snapshot_page_is_refused(self):
        store = self.store()
        self.capture(store, "native-1", "corrupted page marker")
        self.sync(store)
        snapshot = self.snapshots()[0]["id"]
        status, _headers, _body = self.stack.r2_put("v1/s/origin-a/%s/0" % snapshot, b"corrupted page")
        self.assertEqual(status, 200)
        with self.assertRaises(mirror.MirrorError):
            self.verify(objects=True)

    def test_backoff_under_repeated_faults_is_bounded(self):
        self.restart(("--fault-every", "2", "--fault-statuses", "429,500,503"))
        store = self.store()
        self.capture(store, "native-1", "backoff marker")
        delays = []
        result = self.sync(store, sleep=delays.append)
        self.assertEqual(result["generations_committed"], 1)
        self.assertGreater(len(delays), 0)
        self.assertLessEqual(max(delays), 60)
        self.assertTrue(all(delay >= 1 for delay in delays))

    def test_a_client_restart_mid_sync_finishes(self):
        self.restart(("--drop-request-path", "/api/v1/sync/objects/", "--drop-request-method", "PUT"))
        store = self.store()
        self.capture(store, "native-1", "client restart marker")
        client = mirror.MirrorClient(
            self.config, attempts=1,
            factory=lambda: http.client.HTTPSConnection(
                "127.0.0.1", self.stack.port, timeout=30, context=self.stack.tls),
            sleep=self.no_sleep)
        try:
            with self.assertRaises(mirror.MirrorError):
                mirror.sync(
                    store, self.config, "cloudflare", client=client,
                    upload_client_factory=lambda: mirror.MirrorClient(
                        self.config, attempts=1,
                        factory=lambda: http.client.HTTPSConnection(
                            "127.0.0.1", self.stack.port, timeout=30, context=self.stack.tls),
                        sleep=self.no_sleep),
                    recipients=self.recipients, concurrency=1)
        finally:
            client.close()
        result = self.sync(store, sleep=self.no_sleep)
        self.assertEqual(result["generations_committed"], 1)
        self.assertEqual(self.stack.d1("SELECT count(*) AS count FROM generation", mode="first")["count"], 1)
        self.assertEqual(self.stack.d1(
            "SELECT count(*) AS count FROM generation_object", mode="first")["count"], 1)
        self.assertEqual(self.drops(), 1)


class DevModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_age()
        subprocess.run(
            ["make", "reset-local"], cwd=str(ROOT), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        cls.root = Path(cls.temp.name)
        cls.empty = cls.root / "empty"
        cls.empty.mkdir()
        cls.state = cls.root / "state"
        store = Store(cls.state)
        try:
            with store.db:
                store.db.execute(
                    "INSERT OR REPLACE INTO collector_identity(singleton, id, origin_host, display_label, created_at) "
                    "VALUES(1, 'synthetic', 'origin-a', 'synthetic', 0)")
            source = cls.root / "native-1.jsonl"
            source.write_text(json.dumps({
                "type": "message", "id": "m-native-1", "parentId": None,
                "timestamp": "2026-10-30T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "dev mode needle"}],
                            "meta": {"messageId": "m-native-1"}},
            }, separators=(",", ":")) + "\n")
            archive_module.capture(store.db, store.archive_dir, "codex", "native-1", source)
        finally:
            store.close()
        cls.env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "AGE_BIN") if key in os.environ}
        cls.env["TMPDIR"] = str(WORK_TMP)
        cls.process = None

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        subprocess.run(
            ["make", "reset-local"], cwd=str(ROOT), check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.temp.cleanup()

    @classmethod
    def start(cls):
        endpoint_file = DEV_ROOT / "endpoint.json"
        if endpoint_file.is_file():
            endpoint_file.unlink()
        cls.process = subprocess.Popen(
            ["make", "dev-local"], cwd=str(ROOT), env=cls.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        stdout = cls.process.stdout
        if stdout is None:
            raise AssertionError("dev-local has no output stream")
        ready, _, _ = select.select([stdout], [], [], 60)
        if not ready:
            cls.stop()
            raise AssertionError("dev-local did not start")
        line = stdout.readline()
        try:
            endpoint = json.loads(line)
        except ValueError:
            cls.stop()
            raise AssertionError("dev-local did not start: " + (line.strip() or "timeout")) from None
        if not endpoint_file.is_file():
            cls.stop()
            raise AssertionError("dev-local did not write endpoint.json")
        return endpoint

    @classmethod
    def stop(cls):
        process, cls.process = cls.process, None
        if process is None:
            return
        group = process.pid
        if process.poll() is None:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if not wait_until(lambda: not port_open(PORT), 20):
            raise AssertionError("dev-local left port %d open" % PORT)

    def sync(self, endpoint):
        env = dict(self.env)
        env["HOME"] = endpoint["home"]
        env["SSL_CERT_FILE"] = endpoint["ca_file"]
        result = subprocess.run(
            [*testkit.command("agent-sessions"), "mirror", "sync",
             "--state-dir", str(self.state), "--root", str(self.empty), "--json"],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_dev_mode_syncs_restarts_and_resets(self):
        first_endpoint = self.start()
        self.assertTrue(first_endpoint["url"].startswith("https://127.0.0.1:"))
        self.assertEqual(set(first_endpoint["token_files"]), {"origin-a", "origin-b"})
        first = self.sync(first_endpoint)
        self.assertEqual(first["generations_committed"], 1)
        self.stop()

        second_endpoint = self.start()
        self.assertEqual(second_endpoint["home"], first_endpoint["home"])
        second = self.sync(second_endpoint)
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["generations_committed"], 0)
        self.stop()

        subprocess.run(["make", "reset-local"], cwd=str(ROOT), check=True)
        self.assertFalse(DEV_ROOT.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
