#!/usr/bin/env python3
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
SRC_DIR = ROOT / "agent-memory" / "src"
sys.path.insert(0, str(SRC_DIR))
MODULE_PATH = SRC_DIR / "agent_memory" / "store.py"
SPEC = importlib.util.spec_from_file_location("agent_session_store", MODULE_PATH)
assert SPEC is not None
STORE_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = STORE_MODULE
SPEC.loader.exec_module(STORE_MODULE)
Store = STORE_MODULE.Store
StoreError = STORE_MODULE.StoreError
project_identity = STORE_MODULE.project_identity

ARCHIVE_SPEC = importlib.util.spec_from_file_location("agent_memory_archive", SRC_DIR / "agent_memory" / "archive.py")
assert ARCHIVE_SPEC is not None
ARCHIVE = importlib.util.module_from_spec(ARCHIVE_SPEC)
assert ARCHIVE_SPEC.loader is not None
sys.modules[ARCHIVE_SPEC.name] = ARCHIVE
ARCHIVE_SPEC.loader.exec_module(ARCHIVE)

LINE = ('{"timestamp":"2026-10-30T00:00:00Z","type":"response_item","payload":'
        '{"type":"message","role":"user","content":[{"type":"input_text","text":"%s"}]}}\n')


def line(text):
    return LINE % text


def header(cwd, session="a"):
    return json.dumps({"timestamp": "2026-10-30T00:00:00Z", "type": "session_meta",
                       "payload": {"id": session, "cwd": str(cwd)}}, separators=(",", ":")) + "\n"


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.project_id = project_identity(self.project)
        self.path = self.sessions / "rollout-a.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, text):
        self.path.write_text(header(self.project) + line(text))

    def refresh(self, store):
        return store.refresh([self.sessions], project=self.project_id)

    def generations(self, store):
        return store.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0]

    def test_legacy_prune_classifies_captured_copies_and_keeps_unknown_files(self):
        self.write("legacy evidence")
        with Store(self.state) as store:
            self.refresh(store)
            legacy = store.archive_dir / "codex" / str(self.path.resolve()).lstrip(os.sep)
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_bytes(self.path.read_bytes())
            unknown = legacy.parent / "unknown.jsonl"
            unknown.write_text("not captured\n")
            rows = {path: redundant for path, _size, redundant in
                    ARCHIVE.legacy_copies(store.db, store.archive_dir)}
        self.assertTrue(rows[legacy])
        self.assertFalse(rows[unknown])

    def test_unchanged_refresh_reads_no_content_and_captures_nothing(self):
        self.write("alpha")
        with Store(self.state) as store:
            first = self.refresh(store)
            self.assertEqual(first["archived"], 1)
            before = self.generations(store)
            noop = self.refresh(store)
            self.assertEqual((noop["bytes_read"], noop["content_opens"]), (0, 0))
            self.assertEqual(self.generations(store), before)
            self.assertEqual(noop["archive_bytes"], 0)

    def test_append_writes_only_the_delta_and_keeps_both_generations(self):
        self.path.write_text(header(self.project) + line("x" * 4090))
        with self.path.open("a") as stream:
            while self.path.stat().st_size < 4 * 1024 * 1024:
                stream.write(line("x" * 4090))
        with Store(self.state) as store:
            self.refresh(store)
            first = self.generations(store)
            appended = line("one more line")
            with self.path.open("a") as stream:
                stream.write(appended)
            stats = self.refresh(store)
            self.assertEqual(self.generations(store), first + 1)
            self.assertLessEqual(stats["archive_bytes"], len(appended) + 65536)
            self.assertLessEqual(stats["bytes_read"], self.path.stat().st_size + 65536)
            self.assertEqual(stats["archive_objects"], 1)
            newest = store.db.execute("SELECT max(id) FROM archive_generation").fetchone()[0]
            self.assertEqual(ARCHIVE.read_generation(store.db, store.archive_dir, newest), self.path.read_bytes())

    def test_equal_size_rewrite_with_restored_timestamp_creates_a_generation(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            first = self.generations(store)
            info = self.path.stat()
            rewritten = self.path.read_text().replace('"alpha"', '"bravo"')
            self.assertEqual(len(rewritten.encode()), info.st_size)
            self.path.write_text(rewritten)
            os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))
            stats = self.refresh(store)
            self.assertEqual(stats["resets"], 1)
            self.assertEqual(self.generations(store), first + 1)
            hit = store.search("bravo", self.project_id)[0]
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["text"], "bravo")
            old = store.db.execute("SELECT min(id) FROM archive_generation").fetchone()[0]
            self.assertEqual(ARCHIVE.read_generation(store.db, store.archive_dir, old).count(b"alpha"), 1)

    def test_truncation_keeps_the_previous_generation(self):
        self.write("first version body")
        with Store(self.state) as store:
            self.refresh(store)
            first = self.generations(store)
            self.path.write_text(header(self.project) + line("short"))
            stats = self.refresh(store)
            self.assertEqual(self.generations(store), first + 1)
            self.assertEqual(stats["resets"], 1)
            oldest = store.db.execute("SELECT min(id) FROM archive_generation").fetchone()[0]
            self.assertIn(b"first version body", ARCHIVE.read_generation(store.db, store.archive_dir, oldest))

    def test_rewriting_an_early_block_then_appending_never_splices_a_stale_prefix(self):
        self.path.write_text(header(self.project) + line("x" * 4090))
        with self.path.open("a") as stream:
            while self.path.stat().st_size < 512 * 1024:
                stream.write(line("x" * 4090))
        with Store(self.state) as store:
            self.refresh(store)
            data = bytearray(self.path.read_bytes())
            data[154:4000] = b"R" * (4000 - 154)
            self.path.write_bytes(bytes(data))
            with self.path.open("a") as stream:
                stream.write(line("after the rewrite"))
            stats = self.refresh(store)
            self.assertEqual(stats["archive_errors"], 0)
            newest = store.db.execute("SELECT max(id) FROM archive_generation").fetchone()[0]
            self.assertEqual(ARCHIVE.read_generation(store.db, store.archive_dir, newest), self.path.read_bytes())

    def test_a_changed_companion_or_native_metadata_creates_a_new_generation_and_never_edits_the_old_one(self):
        self.write("alpha")
        metadata = {"cwd": str(self.project), "parent": None, "source": None, "thread_source": None,
                    "ancestry": "known", "branch_tip": "m-b", "workspaces": None, "step_count": None}
        with Store(self.state) as store:
            stats = {}
            first, created = ARCHIVE.capture(store.db, store.archive_dir, "codex", "a", self.path,
                                             artifacts=[("a.meta.json", b'{"branchPoint":"m-b"}')],
                                             session_metadata=metadata, stats=stats)
            self.assertTrue(created)
            again, created = ARCHIVE.capture(store.db, store.archive_dir, "codex", "a", self.path,
                                             artifacts=[("a.meta.json", b'{"branchPoint":"m-b"}')],
                                             session_metadata=metadata, stats=stats)
            self.assertEqual((again, created), (first, False))
            before = store.db.execute("SELECT * FROM archive_generation WHERE id = ?", (first,)).fetchone()
            artifact = store.db.execute("SELECT name, digest, size FROM archive_artifact WHERE generation = ?", (first,)).fetchone()
            read = stats["bytes_read"]
            second, created = ARCHIVE.capture(store.db, store.archive_dir, "codex", "a", self.path,
                                              artifacts=[("a.meta.json", b'{"branchPoint":"m-a"}')],
                                              session_metadata=metadata, stats=stats)
            self.assertTrue(created)
            self.assertNotEqual(second, first)
            self.assertEqual(stats["bytes_read"], read)
            third, created = ARCHIVE.capture(store.db, store.archive_dir, "codex", "a", self.path,
                                             artifacts=[("a.meta.json", b'{"branchPoint":"m-a"}')],
                                             session_metadata={**metadata, "branch_tip": "m-a"}, stats=stats)
            self.assertTrue(created)
            self.assertEqual(tuple(store.db.execute("SELECT * FROM archive_generation WHERE id = ?", (first,)).fetchone()), tuple(before))
            self.assertEqual(tuple(store.db.execute("SELECT name, digest, size FROM archive_artifact WHERE generation = ?", (first,)).fetchone()), tuple(artifact))
            self.assertEqual(store.read_object_bytes(artifact["digest"]), b'{"branchPoint":"m-b"}')
            for generation in (first, second, third):
                self.assertEqual(ARCHIVE.read_generation(store.db, store.archive_dir, generation), self.path.read_bytes())
            self.assertEqual(store.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 3)

    def test_a_companion_that_flips_back_to_an_earlier_value_is_a_new_latest_generation(self):
        self.write("alpha")
        with Store(self.state) as store:
            ids = [ARCHIVE.capture(store.db, store.archive_dir, "codex", "a", self.path, artifacts=[("a.meta.json", body)])[0]
                   for body in (b"one", b"two", b"one")]
            self.assertEqual(len(set(ids)), 3)
            self.assertEqual(ARCHIVE.last_generation(store.db, ARCHIVE.origin_for(store.db), "codex", "a")["id"], ids[2])

    def test_schema_v8_generations_migrate_with_chunks_artifacts_and_references(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            generation = store.db.execute("SELECT id FROM archive_generation").fetchone()[0]
            digest = store.store_object(b"one")
            store.db.execute("INSERT INTO archive_artifact (generation, name, digest, size) VALUES (?, 'a.meta.json', ?, 3)", (generation, digest))
            store.db.commit()
            with self.path.open("a") as stream:
                stream.write(line("beta"))
            self.refresh(store)
            ids = [row[0] for row in store.db.execute("SELECT id FROM archive_generation ORDER BY id")]
            self.assertEqual(len(ids), 2)
            store.db.execute("INSERT INTO transfer_generation (generation, remote, durable_at, attempts) VALUES (?, 'r', 1, 1)", (ids[-1],))
            store.db.commit()
            store.db.execute("PRAGMA foreign_keys = OFF")
            store.db.executescript(
                "BEGIN;"
                "CREATE TABLE unrelated_parent (id INTEGER PRIMARY KEY);"
                "CREATE TABLE unrelated_child (parent INTEGER REFERENCES unrelated_parent(id));"
                "INSERT INTO unrelated_child VALUES (404);"
                "CREATE TABLE archive_generation_v8 (id INTEGER PRIMARY KEY, origin_host TEXT NOT NULL, harness TEXT NOT NULL, "
                "native_id TEXT NOT NULL, source_path TEXT NOT NULL, source_digest TEXT NOT NULL, source_size INTEGER NOT NULL, "
                "captured_at REAL NOT NULL, protocol INTEGER NOT NULL DEFAULT 1, stored_at REAL, indexed_at REAL, "
                "tombstoned INTEGER NOT NULL DEFAULT 0, source_dev INTEGER, source_ino INTEGER, source_mtime_ns INTEGER, "
                "source_ctime_ns INTEGER, workspace_path TEXT, UNIQUE(origin_host, harness, native_id, source_digest));"
                "INSERT INTO archive_generation_v8 SELECT id, origin_host, harness, native_id, source_path, source_digest, "
                "source_size, captured_at, protocol, stored_at, indexed_at, tombstoned, source_dev, source_ino, source_mtime_ns, "
                "source_ctime_ns, workspace_path FROM archive_generation;"
                "DROP TABLE archive_generation;"
                "ALTER TABLE archive_generation_v8 RENAME TO archive_generation;"
                "PRAGMA user_version = 8;COMMIT;")
            chunks = store.db.execute("SELECT count(*) FROM archive_generation_object WHERE generation = ?", (generation,)).fetchone()[0]
        with Store(self.state) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], STORE_MODULE.SCHEMA_VERSION)
            for row in store.db.execute("SELECT * FROM archive_generation").fetchall():
                self.assertIsNone(row["session_metadata"])
                artifacts = [(item["name"], item["digest"]) for item in store.db.execute(
                    "SELECT name, digest FROM archive_artifact WHERE generation = ?", (row["id"],))]
                self.assertEqual(row["manifest_digest"],
                                 ARCHIVE.manifest_digest(row["source_digest"], row["source_size"], artifacts, None))
            self.assertEqual(store.db.execute("SELECT count(*) FROM transfer_generation").fetchone()[0], 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM archive_generation_object WHERE generation = ?", (generation,)).fetchone()[0], chunks)
            for child in ("archive_generation_object", "archive_artifact", "transfer_generation"):
                self.assertEqual(store.db.execute("PRAGMA foreign_key_check(%s)" % child).fetchall(), [])
            self.assertEqual(store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(ARCHIVE.read_generation(store.db, store.archive_dir, ids[-1]), self.path.read_bytes())
            stats = self.refresh(store)
            self.assertEqual(stats["archived"], 1)
            self.assertEqual(stats["archive_objects"], 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 3)

    def object_files(self, store):
        return sorted(path.name for path in (store.archive_dir / "objects").rglob("*") if path.is_file() and not path.name.startswith("."))

    def cmd_session(self, branch="e0"):
        directory = self.root / "cmd" / "slug"
        directory.mkdir(parents=True, exist_ok=True)
        message = json.dumps({"type": "message", "id": "e0", "parentId": None, "timestamp": "2026-10-30T00:00:00Z",
                              "message": {"role": "user", "content": [{"type": "text", "text": "secret words"}], "meta": {"messageId": "e0"}}})
        (directory / "s1.jsonl").write_text(json.dumps({"type": "session", "version": 3, "id": "s1", "cwd": str(self.project)}) + "\n" + message + "\n")
        (directory / "s1.meta.json").write_text(json.dumps({"branchPoint": branch}))
        return directory / "s1.jsonl"

    def test_a_forgotten_session_is_never_captured_again(self):
        path = self.cmd_session()
        with Store(self.state) as store:
            store.refresh({"cmd": [self.root / "cmd"]}, project=None)
            store.forget_session("cmd:s1", 5.0)
            self.assertEqual(self.generations(store), 0)
            with self.assertRaises(ARCHIVE.ArchiveForgotten):
                ARCHIVE.capture(store.db, store.archive_dir, "cmd", "s1", path)
            stats = store.refresh({"cmd": [self.root / "cmd"]}, project=None)
            self.assertEqual((self.generations(store), stats["archive_errors"]), (0, 0))
            self.assertEqual(self.object_files(store), [])

    def test_forget_records_the_tombstone_before_it_purges_anything(self):
        self.cmd_session()
        with Store(self.state) as store:
            store.refresh({"cmd": [self.root / "cmd"]}, project=None)
            seen = {}
            original = store._purge_generations

            def spy(origin, harness, native):
                seen["tombstoned"] = store.is_tombstoned(origin, harness, native)
                return original(origin, harness, native)

            store._purge_generations = spy
            store.forget_session("cmd:s1", 5.0)
            self.assertTrue(seen["tombstoned"])

    def test_forgetting_a_captured_session_that_was_never_indexed_purges_its_evidence(self):
        path = self.cmd_session()
        with Store(self.state) as store:
            ARCHIVE.capture(store.db, store.archive_dir, "cmd", "s1", path, artifacts=[("s1.meta.json", b"{}")])
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertNotEqual(self.object_files(store), [])
            store.forget_session("cmd:s1", 5.0)
            self.assertEqual(self.generations(store), 0)
            self.assertEqual(self.object_files(store), [])
            self.assertEqual(store.db.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1)

    def test_a_capture_that_fails_midway_leaves_every_object_file_cataloged(self):
        self.write("alpha")
        with self.path.open("a") as stream:
            for _ in range(20):
                stream.write(line("x" * 4000))
        with Store(self.state) as store:
            real = ARCHIVE._write_object
            calls = {"n": 0}

            def growing(archive_dir, data):
                result = real(archive_dir, data)
                calls["n"] += 1
                if calls["n"] == 2:
                    with self.path.open("a") as stream:
                        stream.write(line("appended while capturing"))
                return result

            with mock.patch.object(STORE_MODULE.archive_module, "_write_object", growing):
                stats = self.refresh(store)
            self.assertGreaterEqual(stats["archive_errors"], 1)
            cataloged = {row[0] for row in store.db.execute("SELECT digest FROM archive_object")}
            self.assertEqual(set(self.object_files(store)), cataloged)

    def test_the_header_of_a_session_outside_the_selected_project_keeps_its_sidecar_fields(self):
        self.cmd_session()
        roots = {"cmd": [self.root / "cmd"]}
        with Store(self.state) as store:
            store.refresh(roots, project="git:somewhere-else")
            store.refresh(roots, project="git:somewhere-else")
            tips = [json.loads(row[0])["branch_tip"] for row in store.db.execute("SELECT session_metadata FROM archive_generation")]
            self.assertEqual(tips, ["e0"])

    def test_a_failed_capture_is_visible_in_status(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            with self.path.open("a") as stream:
                stream.write(line("beta"))
            with mock.patch.object(STORE_MODULE.archive_module, "_write_object", side_effect=OSError("disk gone")):
                stats = self.refresh(store)
            self.assertGreaterEqual(stats["archive_errors"], 1)
            self.assertTrue(stats.get("archive_error_detail"))
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM capture_failure").fetchone()[0], 1)
            self.refresh(store)
            self.assertEqual(store.db.execute("SELECT count(*) FROM capture_failure").fetchone()[0], 0)

    def test_capture_refuses_a_symlink_and_a_directory_source(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            link = self.sessions / "link.jsonl"
            link.symlink_to(self.path)
            with self.assertRaises(OSError):
                ARCHIVE.capture(store.db, store.archive_dir, "codex", "linked", link)
            directory = self.sessions / "dir.jsonl"
            directory.mkdir()
            with self.assertRaises(OSError):
                ARCHIVE.capture(store.db, store.archive_dir, "codex", "dir", directory)

    def test_interrupted_publication_leaves_no_partial_generation(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            first = self.generations(store)
            with self.path.open("a") as stream:
                stream.write(line("beta"))
            with mock.patch.object(ARCHIVE, "_fsync_directory", side_effect=OSError("disk gone")):
                with self.assertRaises(OSError):
                    ARCHIVE.capture(store.db, store.archive_dir, "codex", "a", self.path)
            self.assertEqual(self.generations(store), first)
            with self.path.open("a") as stream:
                stream.write(line("gamma"))
            store.refresh([self.sessions], project=self.project_id)
            self.assertEqual(self.generations(store), first + 1)
            newest = store.db.execute("SELECT max(id) FROM archive_generation").fetchone()[0]
            self.assertIn(b"gamma", ARCHIVE.read_generation(store.db, store.archive_dir, newest))

    def test_disk_full_is_reported_and_keeps_the_previous_generation(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            first = self.generations(store)
            with self.path.open("a") as stream:
                stream.write(line("beta"))
            original = os.write
            calls = {"n": 0}

            def failing(descriptor, data):
                calls["n"] += 1
                if calls["n"] > 0:
                    raise OSError(28, "No space left on device")
                return original(descriptor, data)

            with mock.patch.object(ARCHIVE.os, "write", side_effect=failing):
                stats = self.refresh(store)
            self.assertGreaterEqual(stats["archive_errors"], 1)
            self.assertEqual(self.generations(store), first)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual([p for p in (self.state / "archive" / "objects").rglob(".tmp.*")], [])

    def test_concurrent_collectors_publish_distinct_objects(self):
        self.write("alpha")
        results = []

        def collect():
            with Store(self.state) as store:
                store.refresh([self.sessions], project=self.project_id)
                results.append(store.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0])

        threads = [threading.Thread(target=collect) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 3)
        with Store(self.state) as store:
            self.assertEqual(self.generations(store), 1)
            digests = [row[0] for row in store.db.execute("SELECT digest FROM archive_object")]
            self.assertEqual(len(digests), len(set(digests)))
            for digest in digests:
                self.assertTrue(ARCHIVE.object_path(store.archive_dir, digest).is_file())

    def test_capture_never_links_the_source_and_works_on_another_filesystem(self):
        self.write("alpha")
        other = self.root / "other-state"
        with Store(other) as store:
            self.refresh(store)
            source_info = self.path.stat()
            digest = store.db.execute("SELECT digest FROM archive_object").fetchone()[0]
            object_info = ARCHIVE.object_path(store.archive_dir, digest).stat()
            self.assertNotEqual((source_info.st_dev, source_info.st_ino), (object_info.st_dev, object_info.st_ino))
            self.assertEqual(source_info.st_ino, self.path.stat().st_ino)

    def test_evidence_resolves_from_the_generation_after_source_deletion(self):
        self.write("surviving evidence")
        with Store(self.state) as store:
            self.refresh(store)
            hit = store.search("surviving", self.project_id)[0]
            expected = store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["text"]
            self.path.unlink()
            stats = self.refresh(store)
            self.assertEqual(stats["sessions_seen"], 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["text"], expected)

    def test_forget_removes_the_generation_and_records_a_tombstone(self):
        self.write("forget me")
        with Store(self.state) as store:
            self.refresh(store)
            session = store.db.execute("SELECT id FROM sessions").fetchone()[0]
            digest = store.db.execute("SELECT digest FROM archive_object").fetchone()[0]
            store.forget_session(session, 1000.0)
            self.assertEqual(self.generations(store), 0)
            self.assertFalse(ARCHIVE.object_path(store.archive_dir, digest).exists())
            self.assertEqual(store.db.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1)
            self.refresh(store)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_partial_trailing_line_is_captured_but_not_indexed_until_complete(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            with self.path.open("a") as stream:
                stream.write('{"timestamp":"2026-10-30T00:00:01Z","type":"response_item","payload":{"type":"mess')
            stats = self.refresh(store)
            self.assertEqual(stats["partial_files"], 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE kind='user'").fetchone()[0], 1)
            with self.path.open("a") as stream:
                stream.write('age","role":"user","content":[{"type":"input_text","text":"complete"}]}}\n')
            stats = self.refresh(store)
            self.assertEqual(stats["partial_files"], 0)
            hit = store.search("complete", self.project_id)
            self.assertEqual(len(hit), 1)

    def test_a_legacy_path_mirrored_copy_is_adopted_after_the_source_is_gone(self):
        self.write("legacy survivor")
        with Store(self.state) as store:
            self.refresh(store)
            row = store.db.execute("SELECT source_path, harness, native_id FROM sessions").fetchone()
            legacy = store._archived_copy("codex", row["source_path"])
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_bytes(self.path.read_bytes())
            with store.db:
                store.db.execute("UPDATE sessions SET archive_generation = NULL")
                store.db.execute("DELETE FROM archive_generation")
            self.path.unlink()
            stats = self.refresh(store)
            self.assertEqual(stats["sessions_seen"], 0)
            generation = store.db.execute("SELECT archive_generation FROM sessions").fetchone()[0]
            self.assertIsNotNone(generation)
            self.assertIn(b"legacy survivor", ARCHIVE.read_generation(store.db, store.archive_dir, generation))

    def test_legacy_state_moves_to_the_canonical_directory_once(self):
        legacy = self.root / "legacy" / "agent-sessions"
        legacy.mkdir(parents=True)
        (legacy / "sessions.sqlite3").write_text("db")
        canonical = self.root / "home" / ".agent-sessions"
        with mock.patch.object(STORE_MODULE, "legacy_state_dir", lambda: legacy), \
                mock.patch.object(STORE_MODULE, "canonical_state_dir", lambda: canonical):
            self.assertTrue(STORE_MODULE.migrate_legacy_state(canonical))
            self.assertTrue((canonical / "sessions.sqlite3").is_file())
            self.assertFalse(legacy.exists())
            self.assertFalse(STORE_MODULE.migrate_legacy_state(canonical))
            elsewhere = self.root / "elsewhere"
            self.assertFalse(STORE_MODULE.migrate_legacy_state(elsewhere))

    def test_legacy_state_merges_into_a_canonical_container_without_a_session_index(self):
        legacy = self.root / "legacy" / "agent-sessions"
        legacy.mkdir(parents=True)
        (legacy / "sessions.sqlite3").write_text("db")
        (legacy / "archive").mkdir()
        canonical = self.root / "home" / ".agent-sessions"
        (canonical / "state").mkdir(parents=True)
        with mock.patch.object(STORE_MODULE, "legacy_state_dir", lambda: legacy), \
                mock.patch.object(STORE_MODULE, "canonical_state_dir", lambda: canonical):
            self.assertTrue(STORE_MODULE.migrate_legacy_state(canonical))
        self.assertEqual((canonical / "sessions.sqlite3").read_text(), "db")
        self.assertTrue((canonical / "archive").is_dir())
        self.assertTrue((canonical / "state").is_dir())
        self.assertFalse(legacy.exists())

    def test_legacy_state_is_not_merged_over_an_existing_session_index(self):
        legacy = self.root / "legacy" / "agent-sessions"
        legacy.mkdir(parents=True)
        (legacy / "sessions.sqlite3").write_text("legacy")
        canonical = self.root / "home" / ".agent-sessions"
        canonical.mkdir(parents=True)
        (canonical / "sessions.sqlite3").write_text("canonical")
        with mock.patch.object(STORE_MODULE, "legacy_state_dir", lambda: legacy), \
                mock.patch.object(STORE_MODULE, "canonical_state_dir", lambda: canonical):
            with self.assertRaises(StoreError):
                STORE_MODULE.migrate_legacy_state(canonical)
        self.assertEqual((canonical / "sessions.sqlite3").read_text(), "canonical")
        self.assertTrue(legacy.is_dir())

    def test_refresh_relinks_a_migrated_session_to_its_captured_generation(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            generation = store.db.execute("SELECT archive_generation FROM sessions").fetchone()[0]
            with store.db:
                store.db.execute("UPDATE sessions SET archive_generation = NULL")
            self.refresh(store)
            self.assertEqual(store.db.execute("SELECT archive_generation FROM sessions").fetchone()[0], generation)

    def test_a_legacy_session_without_an_origin_gets_the_local_origin(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            origin = store.db.execute("SELECT origin_host FROM collector_identity").fetchone()[0]
            with store.db:
                store.db.execute("UPDATE sessions SET origin_host = NULL")
        with Store(self.state) as store:
            self.assertEqual(store.db.execute("SELECT origin_host FROM sessions").fetchone()[0], origin)
            self.assertEqual(store.db.execute("SELECT count(*) FROM collector_identity").fetchone()[0], 1)

    def test_state_directory_override_beats_the_canonical_default(self):
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_STATE_DIR": str(self.root / "override")}):
            self.assertEqual(STORE_MODULE.default_state_dir(), self.root / "override")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_SESSIONS_STATE_DIR", None)
            self.assertEqual(STORE_MODULE.default_state_dir().name, ".agent-sessions")

    def test_generation_indexing_buffers_one_line_at_a_time(self):
        self.write("alpha")
        with Store(self.state) as store:
            self.refresh(store)
            generation = store.db.execute("SELECT max(id) FROM archive_generation").fetchone()[0]
            state = {"partial": 0}
            lines = list(ARCHIVE.read_generation_lines(store.db, store.archive_dir, generation, state))
            self.assertEqual(len(lines), 2)
            self.assertEqual(state["partial"], 0)
            self.assertTrue(all(raw.endswith(b"\n") for _offset, raw, _length in lines))
            offsets = [offset for offset, _raw, _length in lines]
            self.assertEqual(offsets, sorted(offsets))
            self.assertEqual(offsets[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
