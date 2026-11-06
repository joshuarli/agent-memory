#!/usr/bin/env python3
"""Persistent local stack: dev mode, restart, and reset."""
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402
sys.path.insert(0, str(testkit.SRC))

from agent_memory import archive as archive_module  # noqa: E402
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
            ["sh", str(ROOT / "cloudflare" / "dev-age.sh")],
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
