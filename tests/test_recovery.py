#!/usr/bin/env python3
"""End-to-end recovery: two sources, one central store, one restored host."""
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
SRC_DIR = ROOT / "agent-memory" / "src"
sys.path.insert(0, str(SRC_DIR))
SPEC = importlib.util.spec_from_file_location("agent_session_store", SRC_DIR / "agent_memory" / "store.py")
assert SPEC is not None
STORE_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = STORE_MODULE
SPEC.loader.exec_module(STORE_MODULE)
Store = STORE_MODULE.Store
project_identity = STORE_MODULE.project_identity

for name, path in (("agent_memory_archive", "archive.py"), ("agent_memory_transfer", "transfer.py"),
                   ("agent_memory_backup", "backup.py")):
    spec = importlib.util.spec_from_file_location(name, SRC_DIR / "agent_memory" / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
from agent_memory.learning import Learner  # noqa: E402

ARCHIVE = sys.modules["agent_memory_archive"]
TRANSFER = sys.modules["agent_memory_transfer"]
BACKUP = sys.modules["agent_memory_backup"]


def cmd_line(text):
    return json.dumps({"type": "message", "id": "m-" + text, "parentId": None,
                       "timestamp": "2026-10-30T00:00:00Z",
                       "message": {"role": "user", "content": [{"type": "text", "text": text}],
                                   "meta": {"messageId": "m-" + text}}}, separators=(",", ":")) + "\n"


def agy_line(text):
    return json.dumps({"type": "USER_INPUT", "created_at": "2026-10-30T00:00:00Z", "source": "USER_EXPLICIT",
                       "status": "DONE", "step_index": 0, "content": text}, separators=(",", ":")) + "\n"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.project_id = project_identity(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def source_home(self, name, cmd_text, agy_text, conversation):
        home = self.root / name
        cmd_dir = home / "cmd" / "slug"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "s-1.jsonl").write_text(
            json.dumps({"type": "session", "version": 3, "id": "s-1", "timestamp": "2026-10-30T00:00:00Z",
                        "cwd": str(self.project)}, separators=(",", ":")) + "\n" + cmd_line(cmd_text))
        (cmd_dir / "s-1.meta.json").write_text(json.dumps({"branchPoint": "m-" + cmd_text}))
        brain = home / "agy" / "brain" / conversation / ".system_generated" / "logs"
        brain.mkdir(parents=True)
        (brain / "transcript_full.jsonl").write_text(agy_line(agy_text))
        (brain / "transcript.jsonl").write_text(agy_line(agy_text))
        database = home / "agy" / "conversation_summaries.db"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, workspace_uris TEXT, "
                       "parent_conversation_id TEXT, nesting_depth INTEGER, step_count INTEGER)")
            db.execute("INSERT INTO conversation_summaries VALUES (?, ?, NULL, 0, 1)",
                       (conversation, json.dumps(["file://" + str(self.project)])))
        return home, cmd_dir.parent, brain.parents[3]

    def capture(self, state, home, cmd_root, agy_root):
        store = Store(state)
        store.refresh({"cmd": [cmd_root], "agy": [agy_root]}, project=None)
        return store

    def test_mirror_snapshot_drops_the_rebuilt_search_projection(self):
        state = self.root / "snapshot-source"
        home, cmd_root, agy_root = self.source_home("snapshot", "snapshot evidence", "snapshot agy", "conv-s")
        store = self.capture(state, home, cmd_root, agy_root)
        try:
            self.assertGreater(store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
            destination = self.root / "mirror-snapshot"
            metadata = BACKUP.snapshot(store, destination)
        finally:
            store.close()
        database = destination / "sessions.sqlite3"
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM events_fts").fetchone()[0], 0)
            self.assertGreater(
                connection.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertGreater(metadata["objects_count"], 0)
        self.assertLess(database.stat().st_size, 1_000_000)

    def test_two_sources_one_central_and_a_restored_third_host(self):
        central_state = self.root / "central"
        home, cmd_root, agy_root = self.source_home("a", "alpha evidence", "antigravity alpha", "conv-a")
        store = self.capture(self.root / "state-a", home, cmd_root, agy_root)
        origin_a = ARCHIVE.origin_for(store.db)
        self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 2)
        pending = store.pending_transfers("central")
        self.assertEqual(pending, 2)

        original = store.mark_durable
        store.mark_durable = lambda *args: (_ for _ in ()).throw(OSError("ack dropped"))
        with self.assertRaises(TRANSFER.TransferError):
            TRANSFER.local_sync(store, central_state, "central", origin_a, "a", attempts=1)
        store.mark_durable = original
        self.assertEqual(store.pending_transfers("central"), 2)

        delivered = TRANSFER.local_sync(store, central_state, "central", origin_a, "a")
        self.assertEqual(len(delivered["delivered"]), 2)
        self.assertEqual(store.pending_transfers(delivered["endpoint"]), 0)

        home_b, cmd_root_b, agy_root_b = self.source_home("b", "bravo evidence", "antigravity bravo", "conv-b")
        store_b = self.capture(self.root / "state-b", home_b, cmd_root_b, agy_root_b)
        origin_b = ARCHIVE.origin_for(store_b.db)
        TRANSFER.local_sync(store_b, central_state, "central", origin_b, "b")

        with Store(central_state) as central:
            self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 4)
            hits = central.search("evidence", None)
            self.assertEqual(len(hits), 2)
            origins = {row["origin_host"] for row in hits}
            self.assertEqual(origins, {origin_a, origin_b})
            generation = central.db.execute("SELECT archive_generation FROM sessions WHERE id LIKE '%s-1@%'").fetchone()
            self.assertIsNotNone(generation)

        shutil.rmtree(self.root / "a")
        shutil.rmtree(self.root / "b")

        with Store(central_state) as central:
            central.reindex_generations()
            self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 4)
            hit = central.search("alpha evidence", None)[0]
            evidence = central.resolve_evidence(hit["session"], hit["position"], hit["digest"])
            self.assertEqual(evidence["text"], "alpha evidence")
            shown = central.session_events(hit["session"], position=hit["position"], before=0, after=0)
            self.assertEqual(len(shown), 1)

            hit_agy = central.search("antigravity alpha", None)[0]
            self.assertEqual(central.resolve_evidence(hit_agy["session"], hit_agy["position"], hit_agy["digest"])["text"],
                             "antigravity alpha")
            hit_cmd = central.search("bravo evidence", None)[0]
            self.assertEqual(central.resolve_evidence(hit_cmd["session"], hit_cmd["position"], hit_cmd["digest"])["text"],
                             "bravo evidence")

        with Store(central_state) as central:
            Learner(central)
            with central.db:
                central.db.execute(
                    "INSERT INTO learning_projects (project,cwd,enabled,enabled_at,repository,grant_source,review_harnesses) "
                    "VALUES ('git:canary','/canary',1,1.0,'/canary','project','codex')")
                central.db.execute(
                    "INSERT INTO learning_experiences (project, kind, claim, applies_when, identifiers, fingerprint, basis, "
                    "detector, revision, worktree, observed_at, captured_at, validity, tags, state) "
                    "VALUES ('git:canary','gotcha','claim','cond','[]','fp','deterministic','det','r','w',1,1,'[]','','retained')")

        backup_dir = self.root / "backup"
        with Store(central_state) as central:
            manifest = BACKUP.create(central, backup_dir)
        self.assertEqual(manifest["generations"], 4)

        restored = self.root / "restored"
        BACKUP.restore(backup_dir, restored)
        with Store(restored) as third:
            self.assertEqual(third.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 4)
            self.assertEqual(third.db.execute("SELECT count(*) FROM learning_experiences").fetchone()[0], 1)
            self.assertEqual(third.db.execute("SELECT enabled FROM learning_projects WHERE project='git:canary'").fetchone()[0], 0)
            hit = third.search("alpha evidence", None)[0]
            self.assertEqual(third.resolve_evidence(hit["session"], hit["position"], hit["digest"])["text"], "alpha evidence")
            self.assertEqual(third.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 4)

    def test_forgetting_survives_backup_and_restore(self):
        central_state = self.root / "central"
        home, cmd_root, agy_root = self.source_home("a", "forget this", "keep this", "conv-a")
        store = self.capture(self.root / "source", home, cmd_root, agy_root)
        origin = ARCHIVE.origin_for(store.db)
        TRANSFER.local_sync(store, central_state, "central", origin, "a")
        store.forget_session("cmd:s-1", 5000.0)
        TRANSFER.local_sync(store, central_state, "central", origin, "a")
        with Store(central_state) as central:
            self.assertEqual(central.db.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1)
            self.assertEqual(central.db.execute("SELECT count(*) FROM sessions WHERE id LIKE '%s-1%'").fetchone()[0], 0)
        backup_dir = self.root / "old-backup"
        with Store(central_state) as central:
            BACKUP.create(central, backup_dir)
        restored = self.root / "restored"
        BACKUP.restore(backup_dir, restored)
        with Store(restored) as third:
            self.assertEqual(third.db.execute("SELECT count(*) FROM sessions WHERE id LIKE '%s-1%'").fetchone()[0], 0)
            self.assertEqual(third.db.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1)
            self.assertEqual(len(third.search("keep this", None)), 1)

    def test_restore_does_not_activate_review_grants_or_newer_schema(self):
        central_state = self.root / "central"
        with Store(central_state) as central:
            Learner(central)
            with central.db:
                central.db.execute(
                    "INSERT INTO learning_projects (project,cwd,enabled,enabled_at,repository,grant_source,review_harnesses) "
                    "VALUES ('git:canary','/canary',1,1.0,'/canary','project','codex')")
        backup_dir = self.root / "backup"
        with Store(central_state) as central:
            BACKUP.create(central, backup_dir)
        restored = self.root / "restored"
        BACKUP.restore(backup_dir, restored)
        with Store(restored) as third:
            self.assertEqual(third.db.execute("SELECT count(*) FROM learning_global_grant").fetchone()[0], 0)
        with sqlite3.connect(backup_dir / "sessions.sqlite3") as db:
            db.execute("PRAGMA user_version = 99")
        with self.assertRaises(BACKUP.BackupError):
            BACKUP.restore(backup_dir, self.root / "never")

    def test_restoring_a_pre_forget_backup_with_current_tombstones_stays_forgotten(self):
        central_state = self.root / "central"
        home, cmd_root, agy_root = self.source_home("a", "forget me later", "keep me", "conv-a")
        store = self.capture(self.root / "source", home, cmd_root, agy_root)
        origin = ARCHIVE.origin_for(store.db)
        TRANSFER.local_sync(store, central_state, "central", origin, "a")
        backup_dir = self.root / "pre-forget"
        with Store(central_state) as central:
            BACKUP.create(central, backup_dir)
        store.forget_session("cmd:s-1", 6000.0)
        TRANSFER.local_sync(store, central_state, "central", origin, "a")
        self.assertEqual(Store(central_state).db.execute(
            "SELECT count(*) FROM sessions WHERE id LIKE '%s-1%'").fetchone()[0], 0)
        restored = self.root / "restored"
        BACKUP.restore(backup_dir, restored, tombstones_from=central_state)
        with Store(restored) as third:
            self.assertEqual(third.db.execute("SELECT count(*) FROM sessions WHERE id LIKE '%s-1%'").fetchone()[0], 0)
            self.assertEqual(third.db.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1)
            self.assertEqual(len(third.search("keep me", None)), 1)
            self.assertEqual(third.db.execute("SELECT count(*) FROM archive_generation WHERE native_id = 's-1'").fetchone()[0], 0)
            referenced = {row[0] for row in third.db.execute("SELECT digest FROM archive_object")}
            on_disk = {path.name for path in (restored / "archive" / "objects").rglob("*") if path.is_file()}
            self.assertEqual(on_disk, referenced)


    def backup_source(self):
        home, cmd_root, agy_root = self.source_home("a", "forget me", "keep me", "conv-a")
        return self.capture(self.root / "state", home, cmd_root, agy_root)

    def test_a_backup_that_fails_midway_publishes_nothing(self):
        store = self.backup_source()
        destination = self.root / "backups" / "failed"
        real = BACKUP.archive_module.read_object
        calls = []

        def flaky(archive_dir, digest):
            calls.append(digest)
            if len(calls) == 2:
                raise OSError("disk gone")
            return real(archive_dir, digest)

        with mock.patch.object(BACKUP.archive_module, "read_object", flaky):
            with self.assertRaises(BACKUP.BackupError):
                BACKUP.create(store, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.iterdir()), [])
        with self.assertRaises(BACKUP.BackupError):
            BACKUP.verify(destination)

    def test_a_corrupt_live_object_is_refused_instead_of_copied_into_a_backup(self):
        store = self.backup_source()
        digest = store.db.execute("SELECT digest FROM archive_object LIMIT 1").fetchone()[0]
        path = ARCHIVE.object_path(store.archive_dir, digest)
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        destination = self.root / "backups" / "corrupt"
        with self.assertRaises(BACKUP.BackupError):
            BACKUP.create(store, destination)
        self.assertFalse(destination.exists())

    def test_a_concurrent_forget_cannot_remove_an_object_while_a_backup_copies_it(self):
        store = self.backup_source()
        destination = self.root / "backups" / "concurrent"
        real = BACKUP.archive_module.read_object
        race = {"started": False}

        def forget_during_copy(archive_dir, digest):
            if not race["started"]:
                race["started"] = True

                def forget():
                    with Store(self.root / "state") as other:
                        other.forget_session("cmd:s-1", 7000.0)

                race["thread"] = threading.Thread(target=forget)
                race["thread"].start()
                deadline = time.monotonic() + 10
                while store.db.execute("SELECT count(*) FROM archive_generation WHERE native_id = 's-1'").fetchone()[0]:
                    self.assertLess(time.monotonic(), deadline, "the forget never reached the catalog")
                    time.sleep(0.01)
                race["blocked"] = race["thread"].is_alive()
            return real(archive_dir, digest)

        with mock.patch.object(BACKUP.archive_module, "read_object", forget_during_copy):
            manifest = BACKUP.create(store, destination)
        race["thread"].join(10)
        self.assertTrue(race["blocked"])
        self.assertFalse(race["thread"].is_alive())
        self.assertEqual(BACKUP.verify(destination)["objects"], manifest["objects"])
        with sqlite3.connect(destination / "sessions.sqlite3") as snapshot:
            self.assertGreaterEqual(snapshot.execute("SELECT count(*) FROM archive_generation WHERE native_id = 's-1'").fetchone()[0], 1)
        store.db.rollback()
        live = {path.name for path in (self.root / "state" / "archive" / "objects").rglob("*") if path.is_file()}
        self.assertEqual(live, {row[0] for row in store.db.execute("SELECT digest FROM archive_object")})

    def test_an_empty_destination_directory_is_accepted_and_a_populated_one_is_refused(self):
        store = self.backup_source()
        empty = self.root / "empty"
        empty.mkdir()
        BACKUP.create(store, empty)
        self.assertTrue((empty / "manifest.json").is_file())
        with self.assertRaises(BACKUP.BackupError):
            BACKUP.create(store, empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
