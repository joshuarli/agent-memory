#!/usr/bin/env python3
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
SRC_DIR = ROOT / "agent-memory" / "src"
sys.path.insert(0, str(SRC_DIR))
SPEC = importlib.util.spec_from_file_location("agent_session_store", SRC_DIR / "agent_memory" / "store.py")
STORE_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = STORE_MODULE
SPEC.loader.exec_module(STORE_MODULE)
Store = STORE_MODULE.Store
project_identity = STORE_MODULE.project_identity
CLI = ROOT / "dotfiles" / "base" / ".local" / "bin" / "agent-sessions"

ARCHIVE_SPEC = importlib.util.spec_from_file_location("agent_memory_archive", SRC_DIR / "agent_memory" / "archive.py")
ARCHIVE = importlib.util.module_from_spec(ARCHIVE_SPEC)
assert ARCHIVE_SPEC.loader is not None
sys.modules[ARCHIVE_SPEC.name] = ARCHIVE
ARCHIVE_SPEC.loader.exec_module(ARCHIVE)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def test_identity_is_random_and_regeneration_keeps_history(self):
        with Store(self.state) as store:
            first = ARCHIVE.ensure_identity(store.db, label="laptop")[0]
            self.assertEqual(len(first), 32)
            self.assertNotIn("host", first.lower())
            second = ARCHIVE.ensure_identity(store.db, label="copy", regenerate=True)[0]
            self.assertNotEqual(first, second)
            origins = {row[0] for row in store.db.execute("SELECT id FROM origin_host")}
            self.assertEqual(origins, {first, second})
            self.assertEqual(store.db.execute("SELECT count(*) FROM collector_identity").fetchone()[0], 1)

    def test_project_mapping_is_explicit_and_same_basename_is_not_merged(self):
        first = self.root / "one" / "app"
        second = self.root / "two" / "app"
        for path in (first, second):
            path.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(path)], check=True)
        self.assertNotEqual(project_identity(first), project_identity(second))
        with Store(self.state) as store:
            origin = ARCHIVE.origin_for(store.db)
            with store.db:
                store.db.execute("INSERT INTO project_map (id, origin_host, name, checkout_path, created_at) "
                                 "VALUES (?, ?, 'shared', ?, 1.0)", (origin + ":shared", origin, str(first)))
            self.assertEqual(store._mapped_project(origin, str(first)), "mapped:shared")
            self.assertIsNone(store._mapped_project(origin, str(second)))

    def test_central_never_runs_git_in_a_foreign_checkout(self):
        from unittest import mock

        with Store(self.state) as store:
            with mock.patch.object(subprocess, "run", side_effect=AssertionError("git must not run")):
                self.assertIsNone(store._mapped_project("foreign", "/somewhere/else"))
                self.assertIsNone(store._mapped_project("foreign", ""))

    def test_a_central_qualified_id_forgets_the_local_session(self):
        with Store(self.state) as store:
            origin = ARCHIVE.origin_for(store.db)
            with store.db:
                store.db.execute(
                    "INSERT INTO sessions (id, project, cwd, harness, source_path, updated, file_size, file_mtime_ns, "
                    "file_ctime_ns, file_dev, file_ino, indexed_bytes, header_digest, coverage, native_id, ancestry, origin_host) "
                    "VALUES ('cmd:local', NULL, '/w', 'cmd', 'generation:1', 1, 0, 0, 0, 0, 0, 0, '', 'complete', 'local', 'known', ?)",
                    (origin,))
            store.forget_session("cmd:local@" + origin, 1000.0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT native_id FROM tombstone").fetchone()[0], "local")

    def test_bare_identifier_is_not_silently_matched_on_a_central_store(self):
        for origin in ("a" * 32, "b" * 32):
            with Store(self.state) as store:
                with store.db:
                    store.db.execute(
                        "INSERT INTO sessions (id, project, cwd, harness, source_path, updated, file_size, file_mtime_ns, "
                        "file_ctime_ns, file_dev, file_ino, indexed_bytes, header_digest, coverage, native_id, ancestry, origin_host) "
                        "VALUES (?, NULL, '/w', 'codex', 'generation:1', 1, 0, 0, 0, 0, 0, 0, '', 'complete', 'same', 'known', ?)",
                        ("same@" + origin, origin))
        with Store(self.state) as store:
            with self.assertRaises(STORE_MODULE.StoreError) as bare:
                store.session_events("same")
            self.assertIn("not indexed", str(bare.exception))
            with self.assertRaises(STORE_MODULE.StoreError) as qualified:
                store.session_events("same@" + "a" * 32)
            self.assertIn("no complete indexed events", str(qualified.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
