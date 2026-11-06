#!/usr/bin/env python3
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "agent-memory" / "src"))
MODULE_PATH = ROOT / "agent-memory" / "src" / "agent_memory" / "store.py"
SPEC = importlib.util.spec_from_file_location("agent_session_store", MODULE_PATH)
assert SPEC is not None
STORE_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = STORE_MODULE
SPEC.loader.exec_module(STORE_MODULE)
Store = STORE_MODULE.Store
StoreError = STORE_MODULE.StoreError
active_branch = STORE_MODULE.active_branch
from agent_memory.transcripts import canonical_harness  # noqa: E402
normalize = STORE_MODULE.normalize
project_identity = STORE_MODULE.project_identity
redact = STORE_MODULE.redact
rebuild_index = STORE_MODULE.rebuild_index
CLI_PATH = ROOT / "dotfiles" / "base" / ".local" / "bin" / "agent-sessions"


class LauncherContractTests(unittest.TestCase):
    def test_launcher_python_floor_matches_project_metadata(self):
        metadata = tomllib.loads((ROOT / "agent-memory" / "pyproject.toml").read_text())
        requirement = metadata["project"]["requires-python"]
        match = re.fullmatch(r">=([0-9]+)\.([0-9]+)", requirement)
        if match is None:
            self.fail("agent-memory pyproject.toml must declare a numeric Python minimum")
        major, minor = (int(part) for part in match.groups())
        constraint = ">=%d.%d" % (major, minor)

        for command in ("agent-learn", "agent-sessions"):
            launcher = ROOT / "dotfiles" / "base" / ".local" / "bin" / command
            source = launcher.read_text()
            with self.subTest(command=command):
                self.assertIn("if sys.version_info < (%d, %d):" % (major, minor), source)
                self.assertIn('"--python", "%s"' % constraint, source)


def metadata(session, cwd, parent=None, source="cli", thread_source="user"):
    payload = {"id": session, "cwd": str(cwd), "source": source, "thread_source": thread_source}
    if parent:
        payload["parent_thread_id"] = parent
    return {"type": "session_meta", "payload": payload}


def message(text, role="user", timestamp="2026-10-23T12:00:00Z"):
    return {
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]},
    }


def line(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.transcripts = self.root / "sessions"
        self.transcripts.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.project_id = project_identity(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def transcript(self, session="s1", cwd=None, records=(), root=None, name=None):
        path_root = Path(root) if root else self.transcripts
        path_root.mkdir(parents=True, exist_ok=True)
        path = path_root / (name or ("rollout-" + session + ".jsonl"))
        path.write_bytes(b"".join(line(record) for record in (metadata(session, cwd or self.project), *records)))
        return path

    def test_normalize_keeps_text_call_provenance_and_unknown_variants(self):
        codex = normalize({
            "type": "response_item", "timestamp": "now",
            "payload": {"type": "custom_tool_call", "name": "Read", "call_id": "c1", "input": {"path": "src/main.py"}},
        })[0]
        self.assertEqual(codex["kind"], "tool-call")
        self.assertEqual(codex["call_id"], "c1")
        self.assertIn("src/main.py", codex["text"])
        unknown = normalize({"type": "response_item", "payload": {"type": "future_event", "detail": "retained"}})[0]
        self.assertEqual(unknown["kind"], "response_item:future_event")
        self.assertIn("retained", unknown["text"])
        self.assertEqual(normalize({"type": "user", "content": "hello"}, "claude")[0]["text"], "hello")
        self.assertEqual(normalize({"type": "user", "content": ["hello"]}, "grok")[0]["text"], "hello")
        self.assertEqual(normalize({"payload": {"event": {"kind": "model_user_messages", "messages": ["hello"]}}}, "muse")[0]["text"], "hello")

    def test_muse_tool_turns_keep_commands_results_and_exit_outcomes(self):
        def event(kind, **fields):
            return {"payload": {"kind": "run", "event": {"kind": kind, **fields}}}

        call = normalize(event("assistant_tool_calls_committed", tool_calls=[
            {"id": "fc_1", "call_id": "call_1", "name": "bash", "args": json.dumps({"command": "cargo test"})},
            {"id": "fc_2", "call_id": "call_2", "name": "read", "args": json.dumps({"path": "src/lib.rs"})},
        ]), "muse")[0]
        self.assertEqual((call["kind"], call["call_id"], call["tool_name"]), ("tool-call", "call_1,call_2", "bash"))
        self.assertIn("cargo test", call["text"])
        failed = normalize(event("tool_result_batch_committed", results=[
            {"tool_call_id": "call_1", "text": json.dumps({"command": "cargo test", "exit_code": 101, "output": "boom"})},
        ]), "muse")[0]
        self.assertEqual((failed["kind"], failed["call_id"], failed["outcome"]), ("tool-output", "call_1", "error"))
        self.assertIn("boom", failed["text"])
        passed = normalize(event("tool_result_batch_committed", results=[
            {"tool_call_id": "call_1", "text": json.dumps({"exit_code": 0, "output": "fine"})},
        ]), "muse")[0]
        self.assertEqual(passed["outcome"], "ok")
        rejected = normalize(event("tool_result_batch_committed", results=[
            {"tool_call_id": "call_3", "text": '<read-skill-result name="x" status="error">'},
        ]), "muse")[0]
        self.assertEqual(rejected["outcome"], "error")
        said = normalize(event("assistant_message_committed", text="Cleanup verified."), "muse")[0]
        self.assertEqual((said["kind"], said["text"]), ("assistant", "Cleanup verified."))
        self.assertEqual(normalize(event("reasoning_committed", text="", encrypted_content="x"), "muse")[0]["kind"], "reasoning")

    def test_runtime_bookkeeping_kinds_are_recognized_not_degraded(self):
        for harness, obj in (
            ("claude", {"type": "api-request-shape"}),
            ("claude", {"type": "cost-state"}),
            ("claude", {"type": "tools-used"}),
            ("grok", {"type": "backend_tool_call"}),
            ("muse", {"payload": {"kind": "task", "event": {"kind": "output", "chunk": "dup"}}}),
            ("muse", {"payload": {"kind": "run", "event": {"kind": "model_completed"}}}),
            ("muse", {"payload": {"kind": "scheduled"}}),
            ("muse", {"retained_marker": "omitted_live_only", "omitted_record": {"omission_class": "task_tool_delta_v1"}}),
            ("muse", {"retained_frame": "session_permission_transaction", "children": []}),
            ("muse", {"payload_type": "runtime.session.task_source_committed", "payload": {"task_id": "t"}}),
            ("muse", {"payload_type": "runtime.user_intent.accepted", "payload": {}}),
            ("muse", {"payload_type": "session.name.changed", "payload": {"name": "x"}}),
        ):
            with self.subTest(harness=harness, obj=obj):
                self.assertFalse(normalize(obj, harness)[0]["kind"].startswith("unknown:"))

    def test_project_refresh_limits_body_reads_and_preserves_other_project(self):
        other = self.root / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.transcript("a1", self.project, [message("almanacneedle")])
        self.transcript("b1", other, [message("unrelatedneedle")], name="rollout-b1.jsonl")
        with Store(self.state) as store:
            first = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(first["sessions_indexed"], 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(store.search("almanacneedle", self.project_id)[0]["session"], "a1")
            self.assertEqual(store.search("unrelatedneedle", self.project_id), [])
            other_id = project_identity(other)
            second = store.refresh([self.transcripts], project=other_id)
            self.assertGreater(second["content_opens"], 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 2)
            self.assertEqual(store.search("almanacneedle", other_id), [])

    def test_projects_refresh_reuses_metadata_for_shared_roots(self):
        others = []
        project_ids = [self.project_id]
        for index in range(2):
            project = self.root / ("other-%d" % index)
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            others.append(project)
            project_ids.append(project_identity(project))
        for index, project in enumerate((self.project, *others)):
            self.transcript("shared-%d" % index, project, [message("sharedneedle%d" % index)])
        with Store(self.state) as store:
            first = store.refresh([self.transcripts], projects=project_ids)
            self.assertEqual(first["sessions_seen"], 3)
            self.assertEqual(first["sessions_indexed"], 3)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 3)
            before = store.db.total_changes
            noop = store.refresh([self.transcripts], projects=iter(project_ids))
            self.assertEqual(noop["stat_operations"], 3)
            self.assertEqual(noop["content_opens"], 0)
            self.assertEqual(noop["bytes_read"], 0)
            self.assertEqual(noop["git_processes"], 0)
            self.assertEqual(noop["sqlite_changes"], 0)
            self.assertEqual(store.db.total_changes, before)
            selected = store.refresh([self.transcripts], projects={project_ids[1]})
            self.assertEqual(selected["sessions_seen"], 3)
            self.assertEqual(selected["sessions_indexed"], 1)
            self.assertEqual(selected["skipped_project"], 2)
            self.assertEqual(selected["content_opens"], 0)
            with self.assertRaisesRegex(ValueError, "either project or projects"):
                store.refresh([self.transcripts], project=project_ids[0], projects=project_ids)
            moved = self.root / "moved"
            moved.mkdir()
            subprocess.run(["git", "init", "-q", str(moved)], check=True)
            moved_path = self.transcripts / "rollout-shared-0.jsonl"
            moved_path.write_bytes(line(metadata("shared-0", moved)) + line(message("movedneedle")))
            moved_stats = store.refresh([self.transcripts], projects={project_ids[0]})
            self.assertEqual(moved_stats["deleted"], 1)
            self.assertIsNone(store.db.execute("SELECT 1 FROM sessions WHERE id='shared-0'").fetchone())

    def test_unindexed_header_cache_avoids_disabled_and_worker_content_reads(self):
        other_projects = []
        project_ids = [self.project_id]
        for index in range(2):
            project = self.root / ("cache-project-%d" % index)
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            other_projects.append(project)
            project_ids.append(project_identity(project))
        projects = [self.project, *other_projects]
        for project_index, project in enumerate(projects):
            for session_index in range(2):
                self.transcript(
                    "cached-%d-%d" % (project_index, session_index),
                    project,
                    [message("cachemarker%d%d" % (project_index, session_index))],
                )
        worker = self.transcripts / "rollout-cache-worker.jsonl"
        worker.write_bytes(
            line(metadata("cache-worker", self.project, source={"learning_worker": True}))
            + line(message("workerbodymustnotindex"))
        )
        with Store(self.state) as store:
            first = store.refresh([self.transcripts], projects={project_ids[0]})
            self.assertEqual(first["sessions_indexed"], 2)
            self.assertEqual(first["skipped_learning_workers"], 1)
            self.assertEqual(first["content_opens"], 9)
            self.assertEqual(first["git_processes"], 3)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 2)
            self.assertEqual(store.db.execute("SELECT count(*) FROM unindexed_sources").fetchone()[0], 5)
            self.assertEqual(store.search("workerbodymustnotindex"), [])
            self.assertEqual(store.search("cachemarker10"), [])
            noop = store.refresh([self.transcripts], projects={project_ids[0]})
            self.assertEqual(noop["content_opens"], 0)
            self.assertEqual(noop["bytes_read"], 0)
            self.assertEqual(noop["git_processes"], 0)
            self.assertEqual(noop["sqlite_changes"], 0)
            self.assertEqual(noop["stat_operations"], 7)
            enabled = store.refresh([self.transcripts], projects={project_ids[1]})
            self.assertEqual(enabled["sessions_indexed"], 2)
            self.assertEqual(enabled["content_opens"], 2)
            self.assertEqual(enabled["git_processes"], 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM unindexed_sources").fetchone()[0], 3)
            self.assertEqual(len(store.search("cachemarker10", project_ids[1])), 1)
            self.assertEqual(store.search("cachemarker20"), [])
            self.assertEqual(store.search("workerbodymustnotindex"), [])

    def test_append_partial_malformed_and_noop_refresh(self):
        path = self.transcript("append", records=[message("firstneedle")])
        partial = json.dumps(message("secondneedle"), separators=(",", ":")).encode()
        with path.open("ab") as stream:
            stream.write(partial)
        with Store(self.state) as store:
            first = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(first["partial_files"], 1)
            self.assertEqual(first["content_opens"], 2)
            self.assertEqual(store.db.execute("SELECT partial_bytes FROM sessions WHERE id='append'").fetchone()[0], len(partial))
            self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE session='append'").fetchone()[0], 2)
            partial_noop = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(partial_noop["partial_files"], 1)
            self.assertEqual(partial_noop["content_opens"], 0)
            self.assertEqual(partial_noop["bytes_read"], 0)
            self.assertEqual(partial_noop["sqlite_changes"], 0)
            with path.open("ab") as stream:
                stream.write(b"\nnot-json\n")
            second = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(second["events_added"], 1)
            self.assertEqual(second["content_opens"], 3)
            self.assertEqual(second["malformed_lines"], 1)
            row = store.db.execute("SELECT malformed_lines, partial_bytes, coverage FROM sessions WHERE id='append'").fetchone()
            self.assertEqual(tuple(row), (1, 0, "degraded"))
            changes = store.db.total_changes
            noop = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(noop["events_added"], 0)
            self.assertEqual(noop["content_opens"], 0)
            self.assertEqual(noop["bytes_read"], 0)
            self.assertEqual(noop["git_processes"], 0)
            self.assertEqual(noop["sqlite_changes"], 0)
            self.assertEqual(store.db.total_changes, changes)
            self.assertEqual(noop["malformed_lines"], 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE session='append'").fetchone()[0], 3)

    def test_incomplete_index_progress_retries_with_matching_metadata(self):
        self.transcript("interrupted", records=[message("recoverme")])
        with Store(self.state) as store:
            store.refresh([self.transcripts], project=self.project_id)
            with store.db:
                store.db.execute("DELETE FROM events WHERE session='interrupted'")
                store.db.execute(
                    "UPDATE sessions SET indexed_bytes=0, checkpoint_position=NULL, checkpoint_digest=NULL, partial_bytes=0 "
                    "WHERE id='interrupted'"
                )
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(stats["resets"], 0)
            self.assertEqual(stats["events_added"], 2)
            self.assertEqual(stats["content_opens"], 1)
            self.assertEqual(store.db.execute("SELECT indexed_bytes=file_size FROM sessions WHERE id='interrupted'").fetchone()[0], 1)
            self.assertEqual(len(store.search("recoverme", self.project_id)), 1)

    def test_append_after_eof_before_final_stat_is_read_next_refresh(self):
        path = self.transcript("eof-race", records=[message("beforeappend")])
        appended = line(message("afterappend"))
        original_fstat = STORE_MODULE._fstat_source
        did_append = [False]

        def append_before_final_stat(stream, stats):
            if not did_append[0] and stream.tell() == path.stat().st_size:
                with path.open("ab") as source:
                    source.write(appended)
                did_append[0] = True
            return original_fstat(stream, stats)

        with Store(self.state) as store:
            STORE_MODULE._fstat_source = append_before_final_stat  # ty: ignore[unresolved-attribute] # test monkey-patches the module to observe file reads
            try:
                first = store.refresh([self.transcripts], project=self.project_id)
            finally:
                STORE_MODULE._fstat_source = original_fstat  # ty: ignore[unresolved-attribute] # test restores the monkey-patched module function
            self.assertTrue(did_append[0])
            self.assertEqual(first["events_added"], 2)
            progress = store.db.execute(
                "SELECT indexed_bytes, partial_bytes, file_size FROM sessions WHERE id='eof-race'"
            ).fetchone()
            self.assertLess(progress["indexed_bytes"] + progress["partial_bytes"], progress["file_size"])
            second = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(second["events_added"], 1)
            self.assertEqual(len(store.search("afterappend", self.project_id)), 1)

    def test_same_size_rewrite_with_restored_mtime_resets_index(self):
        path = self.transcript("rewrite", records=[message("oldneedle")])
        with Store(self.state) as store:
            store.refresh([self.transcripts], project=self.project_id)
            previous = path.stat()
            changed = path.read_bytes().replace(b"oldneedle", b"newneedle")
            path.write_bytes(changed)
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(stats["resets"], 1)
            self.assertGreater(stats["content_opens"], 0)
            self.assertEqual(store.search("oldneedle", self.project_id), [])
            self.assertEqual(len(store.search("newneedle", self.project_id)), 1)

    def test_replacement_archive_move_deletion_and_evidence_digest(self):
        path = self.transcript("move", records=[message("oldneedle")])
        archived = self.root / "archive"
        archived.mkdir()
        with Store(self.state) as store:
            store.refresh([self.transcripts, archived], project=self.project_id)
            old = store.search("oldneedle", self.project_id)[0]
            evidence = store.resolve_evidence(old["session"], old["position"], old["digest"])
            self.assertEqual(evidence["text"], "oldneedle")
            path.write_bytes(line(metadata("move", self.project)) + line(message("newneedle")))
            stats = store.refresh([self.transcripts, archived], project=self.project_id)
            self.assertEqual(stats["resets"], 1)
            self.assertEqual(store.search("oldneedle", self.project_id), [])
            self.assertEqual(len(store.search("newneedle", self.project_id)), 1)
            archived_path = archived / path.name
            path.rename(archived_path)
            stats = store.refresh([self.transcripts, archived], project=self.project_id)
            self.assertEqual(stats["resets"], 1)
            self.assertEqual(store.db.execute("SELECT source_path FROM sessions WHERE id='move'").fetchone()[0], str(archived_path.resolve()))
            archived_path.unlink()
            stats = store.refresh([self.transcripts, archived], project=self.project_id)
            self.assertEqual(stats["deleted"], 0)
            self.assertEqual(len(store.search("newneedle", self.project_id)), 1)
            store.forget_session("move", 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)

    def test_parent_source_and_learning_worker_filter(self):
        self.transcript(
            "child",
            records=[message("childneedle")],
            name="rollout-child.jsonl",
        )
        child_path = self.transcripts / "rollout-child.jsonl"
        child_path.write_bytes(
            line(metadata("child", self.project, source={"subagent": {"thread_spawn": {"depth": 1, "parent_thread_id": "root"}}}, thread_source="subagent"))
            + line(message("childneedle"))
        )
        worker = self.transcripts / "rollout-worker.jsonl"
        worker.write_bytes(line(metadata("worker", self.project, source={"learning_worker": True})) + line(message("secretworker")))
        with Store(self.state) as store:
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(stats["skipped_learning_workers"], 1)
            row = store.db.execute("SELECT source, parent, thread_source FROM sessions WHERE id='child'").fetchone()
            self.assertEqual(row["thread_source"], "subagent")
            self.assertEqual(row["parent"], "root")
            self.assertIn('"subagent"', row["source"])
            self.assertEqual(len(store.search("childneedle", self.project_id)), 1)
            self.assertEqual(store.search("secretworker", self.project_id), [])

    def test_rebuild_preserves_learning_tables_and_newer_schema_is_not_destroyed(self):
        self.transcript("s1", records=[message("keepneedle")])
        with Store(self.state) as store:
            store.db.execute("CREATE TABLE learning_hints (id TEXT PRIMARY KEY, text TEXT)")
            store.db.execute("INSERT INTO learning_hints VALUES ('h1', 'keep')")
            store.refresh([self.transcripts], project=self.project_id)
        rebuild_index(self.state)
        with Store(self.state) as store:
            self.assertEqual(store.db.execute("SELECT text FROM learning_hints WHERE id='h1'").fetchone()[0], "keep")
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        path = Store.database_path(self.state)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(StoreError, "newer"):
            Store(self.state)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT text FROM learning_hints WHERE id='h1'").fetchone()[0], "keep")

    @staticmethod
    def downgrade_v4_columns(db, sessions=True):
        db.execute("ALTER TABLE events DROP COLUMN outcome")
        db.execute("ALTER TABLE events DROP COLUMN entry")
        db.execute("ALTER TABLE events DROP COLUMN parent_entry")
        db.execute("ALTER TABLE events DROP COLUMN synthetic_id")
        db.execute("DROP TABLE forgotten_sessions")
        for table in ("tombstone", "transfer_generation", "archive_artifact", "archive_generation_object",
                      "archive_generation", "archive_object", "project_map", "origin_host", "collector_identity"):
            db.execute("DROP TABLE IF EXISTS " + table)
        if sessions:
            for column in ("native_id", "ancestry", "recognized_events", "unrecognized_events",
                           "branch_tip", "workspaces", "step_count", "archive_generation", "origin_host"):
                db.execute("ALTER TABLE sessions DROP COLUMN " + column)

    def test_schema_v1_migrates_metadata_without_rebuilding_index(self):
        self.transcript("legacy", records=[message("legacyevidence")])
        with Store(self.state) as store:
            store.db.execute("CREATE TABLE learning_hints (id TEXT PRIMARY KEY, text TEXT)")
            store.db.execute("INSERT INTO learning_hints VALUES ('h1', 'keep')")
            store.refresh([self.transcripts], project=self.project_id)
        path = Store.database_path(self.state)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("PRAGMA legacy_alter_table = ON")
            self.downgrade_v4_columns(db, sessions=False)
            db.execute("DROP INDEX sessions_project_updated")
            db.execute("DROP INDEX sessions_parent")
            db.execute("DROP TABLE unindexed_sources")
            db.execute("ALTER TABLE sessions RENAME TO sessions_v2")
            db.execute(
                "CREATE TABLE sessions ("
                "id TEXT PRIMARY KEY, project TEXT, cwd TEXT NOT NULL, parent TEXT, source TEXT, "
                "thread_source TEXT, harness TEXT NOT NULL, source_path TEXT NOT NULL, updated REAL NOT NULL, "
                "file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL, file_dev INTEGER NOT NULL, "
                "file_ino INTEGER NOT NULL, indexed_bytes INTEGER NOT NULL DEFAULT 0, header_digest TEXT NOT NULL, "
                "checkpoint_position INTEGER, checkpoint_digest TEXT, malformed_lines INTEGER NOT NULL DEFAULT 0, "
                "partial_bytes INTEGER NOT NULL DEFAULT 0, coverage TEXT NOT NULL DEFAULT 'complete')"
            )
            db.execute(
                "INSERT INTO sessions SELECT id, project, cwd, parent, source, thread_source, harness, source_path, updated, "
                "file_size, file_mtime_ns, file_dev, file_ino, indexed_bytes, header_digest, checkpoint_position, "
                "checkpoint_digest, malformed_lines, partial_bytes, coverage FROM sessions_v2"
            )
            db.execute("DROP TABLE sessions_v2")
            db.execute("CREATE INDEX sessions_project_updated ON sessions(project, updated DESC)")
            db.execute("CREATE INDEX sessions_parent ON sessions(parent)")
            db.execute("PRAGMA user_version = 1")
        with Store(self.state) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], STORE_MODULE.SCHEMA_VERSION)
            self.assertIsNone(store.db.execute("SELECT file_ctime_ns FROM sessions WHERE id='legacy'").fetchone()[0])
            self.assertEqual(store.db.execute("SELECT text FROM learning_hints WHERE id='h1'").fetchone()[0], "keep")
            self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE session='legacy'").fetchone()[0], 2)
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(stats["events_added"], 0)
            self.assertEqual(stats["resets"], 0)
            settled = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual((settled["bytes_read"], settled["content_opens"]), (0, 0))
            self.assertEqual(store.db.execute("SELECT file_ctime_ns FROM sessions WHERE id='legacy'").fetchone()[0], self.transcripts.joinpath("rollout-legacy.jsonl").stat().st_ctime_ns)

    def test_schema_v2_migrates_unindexed_header_cache(self):
        self.transcript("legacy-v2", records=[message("v2evidence")])
        with Store(self.state) as store:
            store.refresh([self.transcripts], project=self.project_id)
        path = Store.database_path(self.state)
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE unindexed_sources")
            self.downgrade_v4_columns(db)
            db.execute("PRAGMA user_version = 2")
        with Store(self.state) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], STORE_MODULE.SCHEMA_VERSION)
            self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE session='legacy-v2'").fetchone()[0], 2)
            self.assertEqual(store.db.execute("SELECT count(*) FROM unindexed_sources").fetchone()[0], 0)
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(stats["events_added"], 0)
            self.assertEqual(stats["resets"], 0)
            settled = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(settled["content_opens"], 0)
            self.assertEqual(settled["bytes_read"], 0)
            self.assertEqual(settled["git_processes"], 0)

    def test_populated_v3_database_from_the_previous_release_migrates_in_place(self):
        ref = "6e301c4"
        shown = subprocess.run(["git", "-C", str(ROOT), "show", ref + ":dotfiles/base/.local/bin/agent_session_store.py"], capture_output=True)
        if shown.returncode:
            self.skipTest("previous release revision is not available in this checkout")
        old_path = self.root / "agent_session_store_v3.py"
        old_path.write_bytes(shown.stdout)
        spec = importlib.util.spec_from_file_location("agent_session_store_v3", old_path)
        assert spec is not None
        old = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        self.transcript("legacy-a", records=[message("legacyneedle one"), message("legacyneedle two", "assistant")])
        self.transcript("legacy-b", records=[message("another legacyneedle")])
        with old.Store(self.state) as store:
            store.db.execute("CREATE TABLE learning_hints (id TEXT PRIMARY KEY, text TEXT)")
            store.db.execute("INSERT INTO learning_hints VALUES ('h1', 'keep')")
            store.db.commit()
            store.refresh([self.transcripts], project=self.project_id)
            before = [tuple(row) for row in store.db.execute("SELECT session, position, digest, kind FROM events ORDER BY session, position")]
        with Store(self.state) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], STORE_MODULE.SCHEMA_VERSION)
            after = [tuple(row) for row in store.db.execute("SELECT session, position, digest, kind FROM events ORDER BY session, position")]
            self.assertEqual(before, after)
            self.assertEqual(store.db.execute("SELECT text FROM learning_hints").fetchone()[0], "keep")
            rows = store.db.execute("SELECT id, native_id, ancestry, coverage, recognized_events FROM sessions ORDER BY id").fetchall()
            self.assertEqual([(row["id"], row["native_id"], row["ancestry"], row["coverage"]) for row in rows], [("legacy-a", "legacy-a", "known", "complete"), ("legacy-b", "legacy-b", "known", "complete")])
            self.assertTrue(all(row["recognized_events"] > 0 for row in rows))
            hit = store.search("legacyneedle", self.project_id)[0]
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["session"], hit["session"])
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual((stats["resets"], stats["events_added"]), (0, 0))
            settled = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual((settled["resets"], settled["events_added"], settled["bytes_read"]), (0, 0, 0))

    def test_schema_v5_migration_reindexes_pi_sessions_to_record_their_branches(self):
        pi_dir = self.root / "pi"
        pi_dir.mkdir()
        header = {"type": "session", "version": 3, "id": "p1", "timestamp": "2026-10-24T23:28:22.773Z", "cwd": str(self.project)}
        entry = {"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-10-24T23:30:00.000Z", "message": {"role": "user", "content": "pimigrate"}}
        (pi_dir / "2026-10-24T23-28-22-773Z_p1.jsonl").write_bytes(line(header) + line(entry))
        self.transcript("keep", records=[message("keepmigrate")])
        roots = {"pi": [pi_dir], "codex": [self.transcripts]}
        with Store(self.state) as store:
            store.refresh(roots, project=self.project_id)
        with sqlite3.connect(Store.database_path(self.state)) as db:
            db.execute("ALTER TABLE events DROP COLUMN entry")
            db.execute("ALTER TABLE events DROP COLUMN parent_entry")
            db.execute("ALTER TABLE events DROP COLUMN synthetic_id")
            for table in ("tombstone", "transfer_generation", "archive_artifact", "archive_generation_object",
                          "archive_generation", "archive_object", "project_map", "origin_host", "collector_identity"):
                db.execute("DROP TABLE IF EXISTS " + table)
            for column in ("branch_tip", "workspaces", "step_count", "archive_generation", "origin_host"):
                db.execute("ALTER TABLE sessions DROP COLUMN " + column)
            db.execute("PRAGMA user_version = 5")
        with Store(self.state) as store:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], STORE_MODULE.SCHEMA_VERSION)
            self.assertEqual([row[0] for row in store.db.execute("SELECT id FROM sessions")], ["keep"])
            store.refresh(roots, project=self.project_id)
            self.assertEqual(store.db.execute("SELECT entry, parent_entry FROM events WHERE session = 'pi:p1' AND kind = 'user'").fetchone()[:], ("u1", None))

    def test_active_branch_drops_abandoned_pi_branches_only(self):
        header = {"type": "session", "version": 3, "id": "branchy", "timestamp": "2026-10-24T23:28:22.773Z", "cwd": str(self.project)}

        def user(entry_id, parent, text):
            return {"type": "message", "id": entry_id, "parentId": parent, "timestamp": "2026-10-24T23:30:00.000Z", "message": {"role": "user", "content": text}}

        pi_dir = self.root / "pi"
        pi_dir.mkdir()
        (pi_dir / "2026-10-24T23-28-22-773Z_branchy.jsonl").write_bytes(b"".join(line(item) for item in (
            header, user("a", None, "root"), user("b", "a", "abandoned"), user("c", "b", "abandoned too"), user("d", "a", "kept"), user("e", "d", "newest"))))
        with Store(self.state) as store:
            store.refresh({"pi": [pi_dir]}, project=self.project_id)

            def texts(rows):
                return [row["text"] for row in rows if row["kind"] == "user"]

            rows = [dict(row) for row in store.db.execute("SELECT * FROM events WHERE session = 'pi:branchy' ORDER BY position")]
            self.assertEqual(texts(rows), ["root", "abandoned", "abandoned too", "kept", "newest"])
            self.assertEqual(texts(STORE_MODULE.active_branch(rows)), ["root", "kept", "newest"])
            self.assertEqual(STORE_MODULE.active_branch(rows)[0]["kind"], "session_meta")
            experience = importlib.import_module("agent_memory.experience")
            events, truncated = experience.session_events(store.db, "pi:branchy")
            self.assertEqual((texts(events), truncated), (["root", "kept", "newest"], False))

    def test_git_worktrees_share_project_identity(self):
        repo = self.root / "git-repo"
        worktree = self.root / "git-worktree"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "file").write_text("one")
        subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-qb", "other", str(worktree)], check=True)
        self.assertEqual(project_identity(repo), project_identity(worktree))

    def test_cli_search_redacts_credentials_and_supports_regex(self):
        token = "ghp_1234567890abcdefghijklmnop"
        self.transcript("cli", records=[message("needle Authorization: Bearer " + token)])
        result = subprocess.run(
            [sys.executable, "-I", str(CLI_PATH), "search", "needle", "--state-dir", str(self.state), "--root", str(self.transcripts)],
            cwd=self.project,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("matches=1", result.stdout)
        self.assertNotIn(token, result.stdout)
        self.assertIn("[REDACTED]", result.stdout)
        self.assertIn("byte=", result.stdout)
        self.assertIn('project="' + str(self.project) + '"', result.stdout)
        with Store(self.state) as store:
            matches = store.search("needle.*Bearer", self.project_id, regex=True)
            self.assertEqual(len(matches), 1)
            position = store.search("needle", self.project_id)[0]["position"]
        shown = subprocess.run(
            [sys.executable, "-I", str(CLI_PATH), "show", "cli", "--position", str(position), "--before", "0", "--after", "0", "--kind", "user", "--since", "2026-10-22", "--state-dir", str(self.state), "--root", str(self.transcripts), "--project", str(self.project)],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("kind=user", shown.stdout)
        recent = subprocess.run(
            [sys.executable, "-I", str(CLI_PATH), "recent", "--kind", "user", "--since", "2026-10-22", "--state-dir", str(self.state), "--root", str(self.transcripts), "--project", str(self.project)],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("sessions=1", recent.stdout)
        doctor = subprocess.run(
            [sys.executable, "-I", str(CLI_PATH), "doctor", "--state-dir", str(self.state)],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("ok sqlite=", doctor.stdout)

    def test_redact_removes_common_secret_assignments(self):
        self.assertEqual(redact("api_key=sk-this-is-a-long-secret-value"), "api_key=[REDACTED]")
        self.assertEqual(redact("Authorization: Bearer abc123"), "Authorization=[REDACTED]")

    def test_search_autoquotes_hyphens_and_slashes(self):
        self.transcript("hyphen", records=[message("release build must have thin lto, codegen-units must not be 1")])
        with Store(self.state) as store:
            store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(len(store.search("codegen-units", self.project_id)), 1)
            self.assertEqual(len(store.search("thin lto / codegen-units", self.project_id)), 1)
            self.assertEqual(len(store.search('"codegen-units"', self.project_id)), 1)

    def test_state_permissions_and_symlinks_are_rejected(self):
        with Store(self.state) as store:
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        target = self.root / "state-target"
        target.mkdir()
        link = self.root / "state-link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(StoreError, "symlink"):
            Store(link)
        known = self.transcript("outside", records=[message("leakmarker")])
        with Store(self.state) as store:
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions WHERE id='outside'").fetchone()[0], 1)
        external = self.root / "outside.jsonl"
        external.write_bytes(line(metadata("outside", self.project)) + line(message("externalmarker")))
        known.unlink()
        known.symlink_to(external)
        with Store(self.state) as store:
            stats = store.refresh([self.transcripts], project=self.project_id)
            self.assertGreater(stats["unreadable"], 0)
            self.assertEqual(store.search("externalmarker", self.project_id), [])
            hit = store.search("leakmarker", self.project_id)[0]
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["text"], "leakmarker")


def opencode_database(root, project, sessions):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "opencode.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE IF NOT EXISTS session_v2 (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, fork_session_id TEXT, "
        "directory TEXT NOT NULL, title TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);"
        "CREATE TABLE IF NOT EXISTS session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL, "
        "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);"
        "CREATE TABLE IF NOT EXISTS credential (id TEXT PRIMARY KEY, label TEXT, value TEXT);"
    )
    for session in sessions:
        db.execute("INSERT OR REPLACE INTO session_v2 VALUES (?, 'p1', ?, ?, ?, 't', 1000, ?)", (session["id"], session.get("parent"), session.get("fork"), str(project), session.get("updated", 2000)))
        for seq, (kind, data) in enumerate(session["messages"], start=1):
            db.execute("INSERT OR REPLACE INTO session_message VALUES (?, ?, ?, ?, 1000, ?, ?)", ("%s-%d" % (session["id"], seq), session["id"], kind, seq, session.get("updated", 2000), json.dumps(data)))
    db.commit()
    db.close()
    return path


def archived_bytes(store):
    return b"".join(path.read_bytes() for path in sorted(store.archive_dir.rglob("*")) if path.is_file())


def oc_user(text):
    return ("user", {"time": {"created": 1792000000000}, "text": text, "files": [], "agents": []})


def oc_assistant(*blocks):
    return ("assistant", {"time": {"created": 1792000001000}, "agent": "build", "content": list(blocks)})


def oc_tool(call, name, tool_input, output, status="completed", **metadata):
    return {"type": "tool", "id": call, "name": name, "executed": True, "state": {"status": status, "input": tool_input, "content": [{"type": "text", "text": output}], "metadata": metadata}}


class OpenCodeIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.data = self.root / "data" / "opencode"
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.project_id = project_identity(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self, store):
        return store.refresh({"opencode": [self.data]}, project=self.project_id)

    def sessions(self) -> list[dict[str, Any]]:
        return [{"id": "ses_alpha", "messages": [
            oc_user("run the focused tests for the cache"),
            oc_assistant(
                {"type": "reasoning", "text": "private thinking"},
                oc_tool("call_1", "shell", {"command": "pytest -q tests/test_cache.py"}, "FAILED tests/test_cache.py::test_evict\nE  KeyError: 'shard'", exit=1, status="completed"),
                {"type": "text", "text": "The shard map is missing a key; patching src/cache.py."},
                oc_tool("call_2", "edit", {"path": "src/cache.py", "oldString": "a", "newString": "b"}, "edited", files=["src/cache.py"]),
                oc_tool("call_3", "shell", {"command": "pytest -q tests/test_cache.py"}, "1 passed in 0.2s", exit=0, status="completed")),
        ]}]

    def test_opencode_sessions_become_generations_and_index_centrally(self):
        opencode_database(self.data, self.project, self.sessions())
        with Store(self.state) as store:
            self.refresh(store)
            row = store.db.execute(
                "SELECT archive_generation FROM sessions WHERE id = 'opencode:ses_alpha'").fetchone()
            self.assertIsNotNone(row["archive_generation"])
            origin = STORE_MODULE.archive_module.origin_for(store.db)
            pending = store.pending_generations("central", origin)
            self.assertEqual(len(pending), 1)
            manifest = STORE_MODULE.archive_module.generation_manifest(store.db, pending[0]["id"])
            received = {
                item["digest"]: store.read_object_bytes(item["digest"])
                for item in [*manifest["chunks"], *manifest["artifacts"]]
            }
            central = Store(self.root / "central")
            try:
                central.commit_generation(manifest, received)
                hits = central.search("KeyError")
                self.assertEqual(len(hits), 1)
                self.assertTrue(hits[0]["session"].startswith("opencode:ses_alpha@"))
                self.assertEqual(central.resolve_evidence(
                    hits[0]["session"], hits[0]["position"], hits[0]["digest"])["kind"], "tool-output")
            finally:
                central.close()
            store.forget_session("opencode:ses_alpha", 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 0)
            self.assertEqual(len(store.pending_tombstones("central", origin)), 1)
            self.assertNotIn(b"KeyError", archived_bytes(store))

    def test_sessions_messages_and_explicit_exit_codes_are_indexed(self):
        opencode_database(self.data, self.project, self.sessions())
        with Store(self.state) as store:
            stats = self.refresh(store)
            self.assertEqual(stats["sessions_indexed"], 1)
            row = store.db.execute("SELECT id, harness, ancestry, thread_source, coverage, native_id FROM sessions").fetchone()
            self.assertEqual(tuple(row), ("opencode:ses_alpha", "opencode", "known", "user", "complete", "ses_alpha"))
            kinds = [tuple(r) for r in store.db.execute("SELECT kind, call_id, outcome FROM events ORDER BY position")]
            self.assertEqual(kinds, [("user", None, None), ("tool-call", "call_1", None), ("tool-output", "call_1", "error"), ("assistant", None, None),
                                     ("tool-call", "call_2", None), ("tool-output", "call_2", "ok"), ("tool-call", "call_3", None), ("tool-output", "call_3", "ok")])
            hits = store.search("KeyError", self.project_id)
            self.assertEqual(hits[0]["session"], "opencode:ses_alpha")
            self.assertEqual(hits[0]["harness"], "opencode")
            self.assertEqual(store.search("private thinking", self.project_id), [])

    def test_evidence_is_verified_against_the_database_row(self):
        path = opencode_database(self.data, self.project, self.sessions())
        with Store(self.state) as store:
            self.refresh(store)
            hit = store.search("KeyError", self.project_id)[0]
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["kind"], "tool-output")
            db = sqlite3.connect(path)
            db.execute("UPDATE session_message SET data = replace(data, 'KeyError', 'ValueError') WHERE seq = 2")
            db.commit()
            db.close()
            with self.assertRaises(STORE_MODULE.EvidenceChanged):
                store.resolve_evidence(hit["session"], hit["position"], hit["digest"])
            path.unlink()
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["kind"], "tool-output")
            store.forget_session(hit["session"], 0)
            with self.assertRaises(STORE_MODULE.EvidenceUnavailable):
                store.resolve_evidence(hit["session"], hit["position"], hit["digest"])

    def test_unchanged_refresh_reads_no_messages_and_a_new_message_reindexes_that_session(self):
        path = opencode_database(self.data, self.project, self.sessions())
        with Store(self.state) as store:
            self.refresh(store)
            noop = self.refresh(store)
            self.assertEqual((noop["content_opens"], noop["bytes_read"], noop["git_processes"], noop["sqlite_changes"]), (0, 0, 0, 0))
            sessions = self.sessions()
            sessions[0]["messages"].append(oc_user("now rerun everything with the race detector"))
            sessions[0]["updated"] = 3000
            opencode_database(self.data, self.project, sessions)
            changed = self.refresh(store)
            self.assertEqual(changed["resets"], 1)
            self.assertEqual(len(store.search("race detector", self.project_id)), 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertTrue(path.exists())

    def test_parent_and_fork_lineage_is_recorded_and_never_invented(self):
        sessions = self.sessions() + [
            {"id": "ses_child", "parent": "ses_alpha", "messages": [oc_user("child work")]},
            {"id": "ses_fork", "fork": "ses_alpha", "messages": [oc_user("forked work")]},
            {"id": "ses_other", "messages": [oc_user("independent work")]},
        ]
        opencode_database(self.data, self.project, sessions)
        with Store(self.state) as store:
            self.refresh(store)
            rows = {r["id"]: r for r in store.db.execute("SELECT id, parent, thread_source FROM sessions")}
            self.assertEqual((rows["opencode:ses_child"]["parent"], rows["opencode:ses_child"]["thread_source"]), ("opencode:ses_alpha", "subagent"))
            self.assertEqual(rows["opencode:ses_fork"]["parent"], "opencode:ses_alpha")
            self.assertIsNone(rows["opencode:ses_other"]["parent"])

    def test_credential_tables_are_never_read_and_the_database_is_not_modified(self):
        path = opencode_database(self.data, self.project, self.sessions())
        db = sqlite3.connect(path)
        db.execute("INSERT INTO credential VALUES ('c1', 'token', 'CREDENTIAL-MARKER-9f3a')")
        db.commit()
        db.close()
        before = path.read_bytes()
        with Store(self.state) as store:
            self.refresh(store)
            dump = json.dumps([list(r) for r in store.db.execute("SELECT text FROM events")]) + json.dumps([list(r) for r in store.db.execute("SELECT * FROM sessions")])
            self.assertNotIn("CREDENTIAL-MARKER", dump)
        self.assertEqual(path.read_bytes(), before)

    def test_symlinked_database_is_not_followed(self):
        real = self.root / "elsewhere"
        opencode_database(real, self.project, self.sessions())
        self.data.mkdir(parents=True)
        os.symlink(real / "opencode.db", self.data / "opencode.db")
        with Store(self.state) as store:
            stats = self.refresh(store)
            self.assertEqual(stats["sessions_indexed"], 0)
            self.assertEqual(store.search("KeyError", self.project_id), [])

    def test_deleted_session_and_unknown_message_types_are_reported(self):
        sessions = self.sessions() + [{"id": "ses_new", "messages": [("hologram", {"time": {"created": 1}, "x": 1})]}]
        path = opencode_database(self.data, self.project, sessions)
        with Store(self.state) as store:
            self.refresh(store)
            coverage = {r["id"]: r["coverage"] for r in store.db.execute("SELECT id, coverage FROM sessions")}
            self.assertEqual(coverage, {"opencode:ses_alpha": "complete", "opencode:ses_new": "unsupported"})
            db = sqlite3.connect(path)
            db.execute("DELETE FROM session_message WHERE session_id = 'ses_alpha'")
            db.execute("DELETE FROM session_v2 WHERE id = 'ses_alpha'")
            db.commit()
            db.close()
            stats = self.refresh(store)
            self.assertEqual(stats["deleted"], 0)
            hit = store.search("KeyError", self.project_id)[0]
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["kind"], "tool-output")
            store.forget_session("opencode:ses_alpha", 0)
            self.refresh(store)
            self.assertEqual(store.search("KeyError", self.project_id), [])
            self.assertNotIn(b"KeyError", archived_bytes(store))

    def test_database_sessions_survive_removal_of_the_database_and_never_archive_credentials(self):
        path = opencode_database(self.data, self.project, self.sessions())
        db = sqlite3.connect(path)
        db.execute("INSERT INTO credential VALUES ('c1', 'label', 'hunter2-secret')")
        db.commit()
        db.close()
        with Store(self.state) as store:
            self.refresh(store)
            hit = store.search("KeyError", self.project_id)[0]
            self.assertNotIn(b"hunter2-secret", archived_bytes(store))
            path.unlink()
            stats = self.refresh(store)
            self.assertEqual(stats["deleted"], 0)
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["kind"], "tool-output")
            self.assertEqual(len(store.search("KeyError", self.project_id)), 1)
            steady = self.refresh(store)
            self.assertEqual((steady["content_opens"], steady["git_processes"], steady["sqlite_changes"]), (0, 0, 0))

    def test_same_native_id_in_another_harness_does_not_collide(self):
        opencode_database(self.data, self.project, self.sessions())
        codex = self.root / "codex"
        codex.mkdir()
        (codex / "rollout-ses_alpha.jsonl").write_bytes(line(metadata("ses_alpha", self.project)) + line(message("codexonly")))
        with Store(self.state) as store:
            store.refresh({"opencode": [self.data], "codex": [codex]}, project=self.project_id)
            ids = sorted(r[0] for r in store.db.execute("SELECT id FROM sessions"))
            self.assertEqual(ids, ["opencode:ses_alpha", "ses_alpha"])


class HarnessIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.project_id = project_identity(self.project)
        self.roots = {name: self.root / "roots" / name for name in ("codex", "claude", "muse", "grok", "pi")}
        for path in self.roots.values():
            path.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(line(record) for record in records))
        return path

    def codex(self, session, text, cwd=None):
        return self.write(self.roots["codex"] / ("rollout-" + session + ".jsonl"), [metadata(session, cwd or self.project), message(text)])

    def claude(self, session, records, directory=None, sidechain=False):
        base = {"sessionId": session, "cwd": str(self.project), "isSidechain": sidechain}
        path = (self.roots["claude"] / "-proj" / (directory or "") / (session + ".jsonl"))
        return self.write(path, [dict(base, **record) for record in records])

    def grok(self, session, records, primary=True):
        directory = self.roots["grok"] / "%2Fproj" / session
        self.write(directory / "chat_history.jsonl", records)
        self.write(directory / "summary.json", [{"info": {"id": session, "cwd": str(self.project)}}])
        if primary:
            self.write(directory / "events.jsonl", [{"type": "turn_started", "session_relationship": "primary"}])
        return directory / "chat_history.jsonl"

    def muse(self, session, records):
        meta = {"payload": {"kind": "metadata", "record": {"workspace_root": str(self.project)}}}
        return self.write(self.roots["muse"] / "2026" / "10" / "20" / session / "session.jsonl", [meta, *records])

    def pi(self, session, records, parent_session=None, directory="--proj--", stamp="2026-10-24T23-28-22-773Z"):
        header = {"type": "session", "version": 3, "id": session, "timestamp": "2026-10-24T23:28:22.773Z", "cwd": str(self.project)}
        if parent_session:
            header["parentSession"] = parent_session
        return self.write(self.roots["pi"] / directory / (stamp + "_" + session + ".jsonl"), [header, *records])

    def refresh(self, store, **kwargs):
        return store.refresh({name: [path] for name, path in self.roots.items()}, project=self.project_id, **kwargs)

    def test_identical_native_ids_stay_distinct_per_harness(self):
        self.codex("shared", "codexneedle")
        self.claude("shared", [{"type": "user", "message": {"role": "user", "content": "claudeneedle"}}])
        self.grok("shared", [{"type": "user", "content": [{"type": "text", "text": "grokneedle"}]}])
        with Store(self.state) as store:
            self.refresh(store)
            ids = {row["id"]: row["harness"] for row in store.db.execute("SELECT id, harness FROM sessions")}
            self.assertEqual(ids, {"shared": "codex", "claude:shared": "claude", "grok:shared": "grok"})
            for needle, expected in (("codexneedle", "shared"), ("claudeneedle", "claude:shared"), ("grokneedle", "grok:shared")):
                hits = store.search(needle, self.project_id)
                self.assertEqual([hit["session"] for hit in hits], [expected])
                evidence = store.resolve_evidence(hits[0]["session"], hits[0]["position"], hits[0]["digest"])
                self.assertEqual(evidence["session"], expected)

    def test_codex_id_that_looks_qualified_cannot_collide(self):
        self.codex("claude:abc", "codexprefixneedle")
        self.claude("abc", [{"type": "user", "message": {"role": "user", "content": "claudeabcneedle"}}])
        with Store(self.state) as store:
            self.refresh(store)
            ids = sorted(row[0] for row in store.db.execute("SELECT id FROM sessions"))
            self.assertEqual(ids, ["claude:abc", "codex:claude:abc"])
            self.assertEqual(store.search("codexprefixneedle", self.project_id)[0]["session"], "codex:claude:abc")

    def test_claude_tool_events_keep_call_ids_and_explicit_outcomes(self):
        self.claude("tools", [
            {"type": "user", "message": {"role": "user", "content": "run the focused test"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q tests/test_x.py"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "1 failed", "is_error": True}]}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": [{"type": "text", "text": "skill body injected by harness"}]}},
        ])
        with Store(self.state) as store:
            self.refresh(store)
            rows = store.db.execute("SELECT kind, call_id, outcome FROM events WHERE session = 'claude:tools' ORDER BY position").fetchall()
            self.assertEqual([tuple(row) for row in rows], [("user", None, None), ("tool-call", "toolu_1", None), ("tool-output", "toolu_1", "error"), ("context", None, None)])

    def test_sidechain_parent_is_known_and_muse_ancestry_is_not_invented(self):
        self.claude("main", [{"type": "user", "message": {"role": "user", "content": "root work"}}])
        self.claude("agent-a1", [{"type": "user", "message": {"role": "user", "content": "child work"}}], directory="main/subagents", sidechain=True)
        self.muse("m1", [{"recorded_at": 1792018406398112, "payload": {"event": {"kind": "model_user_messages", "messages": ["muse prompt"]}}}])
        self.grok("g-primary", [{"type": "user", "content": "grok prompt"}])
        self.grok("g-unknown", [{"type": "user", "content": "grok other"}], primary=False)
        with Store(self.state) as store:
            self.refresh(store)
            rows = {row["id"]: row for row in store.db.execute("SELECT id, parent, ancestry, thread_source FROM sessions")}
            self.assertEqual((rows["claude:main"]["ancestry"], rows["claude:main"]["parent"]), ("known", None))
            self.assertEqual((rows["claude:agent-a1"]["parent"], rows["claude:agent-a1"]["thread_source"]), (None, "subagent"))
            self.assertEqual(rows["muse:m1"]["ancestry"], "unknown")
            self.assertEqual(rows["grok:g-primary"]["ancestry"], "known")
            self.assertEqual(rows["grok:g-unknown"]["ancestry"], "unknown")

    def test_unrecognized_format_is_reported_instead_of_an_empty_success(self):
        self.muse("future", [{"recorded_at": 1, "payload": {"event": {"kind": "model_v9_turn", "data": "x"}}}])
        self.claude("newer", [{"type": "turn_v7", "message": {"role": "user", "content": "new shape"}}])
        with Store(self.state) as store:
            self.refresh(store)
            coverage = {row["id"]: row["coverage"] for row in store.db.execute("SELECT id, coverage FROM sessions")}
            self.assertEqual(coverage, {"muse:future": "unsupported", "claude:newer": "unsupported"})
            kinds = {(row["harness"], row["kind"]) for row in store.unrecognized_kinds()}
            self.assertEqual(kinds, {("muse", "unknown:model_v9_turn"), ("claude", "unknown:turn_v7")})
            summary = {row["harness"]: row for row in store.coverage()}
            self.assertEqual(summary["muse"]["unsupported"], 1)

    def test_pi_tool_calls_results_errors_and_bookkeeping_entries_are_indexed(self):
        def entry(kind, entry_id, parent, **fields):
            return dict({"type": kind, "id": entry_id, "parentId": parent, "timestamp": "2026-10-24T23:30:00.000Z"}, **fields)

        def wrapped(entry_id, parent, role, **fields):
            return entry("message", entry_id, parent, message=dict({"role": role, "timestamp": 1792884611484}, **fields))

        self.pi("pi-1", [
            entry("model_change", "m1", None, provider="google", modelId="gemini-3.8-flash"),
            entry("thinking_level_change", "t1", "m1", thinkingLevel="high"),
            wrapped("s1", "t1", "system", content="", sections={"preamble": "You are an expert coding assistant", "cwd": "/x"}),
            wrapped("u1", "s1", "user", content="find the pineapple setting"),
            wrapped("a1", "u1", "assistant", stopReason="toolUse", content=[
                {"type": "thinking", "thinking": "private reasoning", "thinkingSignature": "sig"},
                {"type": "text", "text": "looking in two places"},
                {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "rg pineapple"}},
                {"type": "toolCall", "id": "call_2", "name": "read", "arguments": {"path": "settings.toml"}},
            ]),
            wrapped("r1", "a1", "toolResult", toolCallId="call_1", toolName="bash", isError=True, content=[{"type": "text", "text": "rg: not found"}]),
            wrapped("r2", "r1", "toolResult", toolCallId="call_2", toolName="read", isError=False, content=[{"type": "text", "text": "pineapple = true"}]),
            wrapped("a2", "r2", "assistant", stopReason="toolUse", content=[
                {"type": "toolCall", "id": "call_3", "name": "bash", "arguments": {"command": "ls"}}]),
            entry("context_edit", "e1", "a2", targetId="r1", replacement=None),
            wrapped("a3", "e1", "assistant", stopReason="error", content=[], errorMessage="429 quota exceeded"),
            entry("compaction", "c1", "a3", summary="earlier work summarized", firstKeptEntryId="u1", tokensBefore=1000),
        ])
        with Store(self.state) as store:
            self.refresh(store)
            rows = store.db.execute("SELECT kind, call_id, outcome FROM events WHERE session = 'pi:pi-1' ORDER BY position").fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                ("session_meta", None, None), ("model_change", None, None), ("thinking_level_change", None, None), ("system", None, None),
                ("user", None, None), ("assistant", "call_1,call_2", None),
                ("tool-output", "call_1", "error"), ("tool-output", "call_2", "ok"),
                ("tool-call", "call_3", None), ("context_edit", None, None), ("error", None, None), ("compacted", None, None),
            ])
            self.assertEqual(store.db.execute("SELECT coverage, unrecognized_events FROM sessions WHERE id = 'pi:pi-1'").fetchone()[:], ("complete", 0))
            self.assertEqual(store.search("private reasoning", self.project_id), [])
            self.assertEqual(store.search("You are an expert", self.project_id), [])
            hit = store.search("rg pineapple", self.project_id)[0]
            self.assertEqual(hit["kind"], "assistant")
            evidence = store.resolve_evidence(hit["session"], hit["position"], hit["digest"])
            self.assertIn("settings.toml", evidence["text"])
            self.assertEqual(store.search("quota exceeded", self.project_id)[0]["kind"], "error")

    def test_pi_child_session_links_its_parent_and_artifact_jsonl_is_not_a_session(self):
        self.pi("root_1", [{"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-10-24T23:30:00.000Z", "message": {"role": "user", "content": "root prompt"}}])
        parent_file = str(self.roots["pi"] / "--proj--" / "2026-10-24T23-28-22-773Z_root_1.jsonl")
        self.pi("child-1", [{"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-10-24T23:31:00.000Z", "message": {"role": "user", "content": "child prompt"}}],
                parent_session=parent_file, stamp="2026-10-24T23-31-00-000Z")
        self.write(self.roots["pi"] / "--proj--" / "subagent-artifacts" / "events.jsonl", [{"type": "subagent.run.started"}])
        with Store(self.state) as store:
            self.refresh(store)
            rows = {row["id"]: row for row in store.db.execute("SELECT id, parent, thread_source, source, ancestry FROM sessions")}
            self.assertEqual(sorted(rows), ["pi:child-1", "pi:root_1"])
            self.assertEqual((rows["pi:root_1"]["parent"], rows["pi:root_1"]["thread_source"]), (None, "user"))
            self.assertEqual((rows["pi:child-1"]["parent"], rows["pi:child-1"]["thread_source"], rows["pi:child-1"]["ancestry"]), ("pi:root_1", None, "known"))

    def test_unchanged_refresh_reads_no_bodies_for_any_harness(self):
        self.codex("c1", "alpha")
        self.claude("cl1", [{"type": "user", "message": {"role": "user", "content": "beta"}}])
        self.grok("g1", [{"type": "user", "content": "gamma"}])
        self.muse("m1", [{"recorded_at": 1792018406398112, "payload": {"event": {"kind": "model_user_messages", "messages": ["delta"]}}}])
        with Store(self.state) as store:
            first = self.refresh(store)
            self.assertEqual(first["sessions_indexed"], 4)
            again = self.refresh(store)
            self.assertEqual((again["content_opens"], again["bytes_read"], again["git_processes"], again["sqlite_changes"]), (0, 0, 0, 0))

    def test_symlinked_transcripts_and_directories_are_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.write(outside / "leak.jsonl", [{"type": "user", "sessionId": "leak", "cwd": str(self.project), "message": {"role": "user", "content": "leakneedle"}}])
        (self.roots["claude"] / "-proj").mkdir()
        os.symlink(outside / "leak.jsonl", self.roots["claude"] / "-proj" / "linked.jsonl")
        os.symlink(outside, self.roots["claude"] / "linked-dir")
        with Store(self.state) as store:
            stats = self.refresh(store)
            self.assertEqual(store.search("leakneedle", self.project_id), [])
            self.assertEqual(stats["sessions_indexed"], 0)

    def test_transcript_growing_between_discovery_and_indexing_is_indexed_not_fatal(self):
        path = self.claude("live", [{"type": "user", "message": {"role": "user", "content": "firstneedle"}}])
        original = STORE_MODULE._metadata

        def grow_after_header(*args, **kwargs):
            result = original(*args, **kwargs)
            with open(path, "ab") as stream:
                stream.write(line({"type": "user", "sessionId": "live", "cwd": str(self.project), "message": {"role": "user", "content": "secondneedle"}}))
            return result

        with Store(self.state) as store, mock.patch.object(STORE_MODULE, "_metadata", grow_after_header):
            self.refresh(store)
            self.assertEqual(len(store.search("firstneedle", self.project_id)), 1)
            self.assertEqual(len(store.search("secondneedle", self.project_id)), 1)

    def test_crash_while_indexing_rolls_back_and_the_next_refresh_completes(self):
        records = [{"type": "user", "message": {"role": "user", "content": "needle%d" % index}} for index in range(6)]
        self.claude("crashy", records)
        calls = {"count": 0}
        original = STORE_MODULE.normalize

        def explode(obj, harness="codex"):
            calls["count"] += 1
            if calls["count"] == 4:
                raise KeyboardInterrupt("simulated crash")
            return original(obj, harness)

        with Store(self.state) as store:
            with mock.patch.object(STORE_MODULE, "normalize", explode), self.assertRaises(KeyboardInterrupt):
                self.refresh(store)
            self.assertEqual(store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
            stats = self.refresh(store)
            self.assertEqual(stats["events_added"], 6)
            self.assertEqual(store.db.execute("SELECT coverage FROM sessions").fetchone()[0], "complete")

    def test_partial_tail_is_flagged_then_indexed_once_the_line_completes(self):
        path = self.claude("tail", [{"type": "user", "message": {"role": "user", "content": "whole"}}])
        full = line({"type": "user", "sessionId": "tail", "cwd": str(self.project), "message": {"role": "user", "content": "latecomer"}})
        with open(path, "ab") as stream:
            stream.write(full[:25])
        with Store(self.state) as store:
            self.refresh(store)
            self.assertEqual(store.db.execute("SELECT coverage, partial_bytes > 0 FROM sessions").fetchone()[:], ("degraded", 1))
            self.assertEqual(store.search("latecomer", self.project_id), [])
            with open(path, "ab") as stream:
                stream.write(full[25:])
            self.refresh(store)
            self.assertEqual(len(store.search("latecomer", self.project_id)), 1)
            self.assertEqual(store.db.execute("SELECT coverage FROM sessions").fetchone()[0], "complete")

    def test_truncated_or_replaced_transcript_resets_instead_of_keeping_stale_events(self):
        records = [{"type": "user", "message": {"role": "user", "content": "oldneedle%d" % index}} for index in range(5)]
        path = self.claude("shrink", records)
        with Store(self.state) as store:
            self.refresh(store)
            self.assertEqual(len(store.search("oldneedle4", self.project_id)), 1)
            path.write_bytes(path.read_bytes()[: len(path.read_bytes()) // 3])
            stats = self.refresh(store)
            self.assertEqual(stats["resets"], 1)
            self.assertEqual(store.search("oldneedle4", self.project_id), [])
            replacement = self.root / "replacement.jsonl"
            replacement.write_bytes(b"".join(line(dict(record, sessionId="shrink", cwd=str(self.project))) for record in [{"type": "user", "message": {"role": "user", "content": "newneedle"}}]))
            os.replace(replacement, path)
            stats = self.refresh(store)
            self.assertEqual(stats["resets"], 1)
            self.assertEqual(len(store.search("newneedle", self.project_id)), 1)
            self.assertEqual(store.search("oldneedle0", self.project_id), [])

    def test_archived_move_keeps_one_session_and_valid_evidence(self):
        path = self.claude("moved", [{"type": "user", "message": {"role": "user", "content": "movedneedle"}}])
        with Store(self.state) as store:
            self.refresh(store)
            hit = store.search("movedneedle", self.project_id)[0]
            archive = self.roots["claude"] / "-archive"
            archive.mkdir()
            os.replace(path, archive / path.name)
            self.refresh(store)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            moved = store.search("movedneedle", self.project_id)[0]
            self.assertEqual(store.resolve_evidence(moved["session"], moved["position"], moved["digest"])["session"], hit["session"])

    def test_two_files_with_one_session_identity_index_once(self):
        self.claude("twin", [{"type": "user", "message": {"role": "user", "content": "twinneedle"}}])
        copy = self.roots["claude"] / "-proj" / "copy" / "twin.jsonl"
        copy.parent.mkdir(parents=True)
        copy.write_bytes((self.roots["claude"] / "-proj" / "twin.jsonl").read_bytes())
        with Store(self.state) as store:
            self.refresh(store)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(len(store.search("twinneedle", self.project_id)), 1)

    def test_corrupt_database_is_refused_and_left_untouched(self):
        with Store(self.state) as store:
            self.claude("c", [{"type": "user", "message": {"role": "user", "content": "x " * 5000}}])
            self.refresh(store)
        path = Store.database_path(self.state)
        data = bytearray(path.read_bytes())
        for offset in range(4096, len(data) - 4096, 4096):
            data[offset:offset + 64] = b"\xff" * 64
        path.write_bytes(bytes(data))
        before = path.read_bytes()
        with self.assertRaises(StoreError):
            with Store(self.state) as store:
                store.refresh({name: [root] for name, root in self.roots.items()})
                store.search("x", None)
                store.db.execute("PRAGMA integrity_check").fetchall()
                raise StoreError("integrity")
        self.assertEqual(path.read_bytes()[:16], before[:16])

    def test_sessions_outlive_the_harness_files_they_came_from(self):
        self.codex("c1", "codexkeep")
        self.claude("cl1", [{"type": "user", "message": {"role": "user", "content": "claudekeep"}}])
        self.grok("g1", [{"type": "user", "content": "grokkeep"}])
        self.muse("m1", [{"recorded_at": 1792018406398112, "payload": {"event": {"kind": "model_user_messages", "messages": ["musekeep"]}}}])
        self.pi("p1", [{"type": "message", "id": "u1", "parentId": None, "timestamp": "2026-10-24T23:30:00.000Z", "message": {"role": "user", "content": "pikeep"}}])
        needles = {"codexkeep": "c1", "claudekeep": "claude:cl1", "grokkeep": "grok:g1", "musekeep": "muse:m1", "pikeep": "pi:p1"}
        with Store(self.state) as store:
            self.refresh(store)
            hits = {needle: store.search(needle, self.project_id)[0] for needle in needles}
            for path in self.roots.values():
                shutil.rmtree(path)
                path.mkdir()
            stats = self.refresh(store)
            self.assertEqual(stats["deleted"], 0)
            for needle, session in needles.items():
                found = store.search(needle, self.project_id)
                self.assertEqual([hit["session"] for hit in found], [session])
                evidence = store.resolve_evidence(session, hits[needle]["position"], hits[needle]["digest"])
                self.assertEqual(evidence["text"], needle)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions WHERE ancestry = 'known'").fetchone()[0], 4)
            steady = self.refresh(store)
            self.assertEqual((steady["content_opens"], steady["bytes_read"], steady["git_processes"], steady["sqlite_changes"]), (0, 0, 0, 0))

    def test_evidence_resolves_from_the_archive_before_the_next_refresh(self):
        path = self.claude("early", [{"type": "user", "message": {"role": "user", "content": "earlyneedle"}}])
        with Store(self.state) as store:
            self.refresh(store)
            hit = store.search("earlyneedle", self.project_id)[0]
            path.unlink()
            self.assertEqual(store.resolve_evidence(hit["session"], hit["position"], hit["digest"])["text"], "earlyneedle")

    def test_session_growing_after_archiving_stays_archived(self):
        path = self.claude("grow", [{"type": "user", "message": {"role": "user", "content": "firstgrow"}}])
        with Store(self.state) as store:
            self.refresh(store)
            with open(path, "ab") as stream:
                stream.write(line({"type": "user", "sessionId": "grow", "cwd": str(self.project), "message": {"role": "user", "content": "secondgrow"}}))
            self.refresh(store)
            path.unlink()
            self.refresh(store)
            self.assertEqual(len(store.search("secondgrow", self.project_id)), 1)
            self.assertEqual(len(store.search("firstgrow", self.project_id)), 1)

    def test_forget_session_purges_the_archived_copy_and_blocks_a_new_one(self):
        live = self.claude("forgotten", [{"type": "user", "message": {"role": "user", "content": "purgeneedle"}}])
        gone = self.claude("vanished", [{"type": "user", "message": {"role": "user", "content": "vanishneedle"}}])
        self.grok("g-forget", [{"type": "user", "content": "grokpurge"}])
        with Store(self.state) as store:
            self.refresh(store)
            self.assertIn(b"purgeneedle", archived_bytes(store))
            gone.unlink()
            self.refresh(store)
            for session in ("claude:forgotten", "claude:vanished", "grok:g-forget"):
                store.forget_session(session, 0)
            for needle in (b"purgeneedle", b"vanishneedle", b"grokpurge"):
                self.assertNotIn(needle, archived_bytes(store))
            self.refresh(store)
            self.assertTrue(live.exists())
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertNotIn(b"purgeneedle", archived_bytes(store))

    def test_symlinked_companions_and_archive_directory_are_refused(self):
        secret = self.root / "secret.json"
        secret.write_text("outsidesecret")
        path = self.grok("g-link", [{"type": "user", "content": "grokentry"}])
        (path.parent / "summary.json").unlink()
        (path.parent / "summary.json").symlink_to(secret)
        with Store(self.state) as store:
            stats = self.refresh(store)
            self.assertEqual(stats["archive_errors"], 1)
            self.assertNotIn(b"outsidesecret", archived_bytes(store))
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        shutil.rmtree(self.state / "archive")
        (self.state / "archive").symlink_to(elsewhere)
        with self.assertRaisesRegex(StoreError, "symlink"):
            Store(self.state)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_learning_workers_and_unindexable_files_are_not_archived(self):
        worker = self.roots["codex"] / "rollout-worker.jsonl"
        self.write(worker, [metadata("worker", self.project, source={"learning_worker": True}), message("workersecret")])
        with Store(self.state) as store:
            self.refresh(store)
            self.assertNotIn(b"workersecret", archived_bytes(store))

    def test_cli_indexes_selected_harness_with_named_root(self):
        self.claude("cli1", [{"type": "user", "message": {"role": "user", "content": "clineedle"}}])
        self.codex("cx", "clineedle")
        result = subprocess.run(
            [sys.executable, str(CLI_PATH), "search", "clineedle", "--state-dir", str(self.state), "--project", str(self.project),
             "--root", "claude=" + str(self.roots["claude"]), "--root", str(self.roots["codex"]), "--harness", "claude"],
            check=True, capture_output=True, text=True)
        self.assertIn("session=claude:cli1", result.stdout)
        self.assertIn("harness=claude", result.stdout)
        self.assertNotIn("session=cx", result.stdout)


CMD_HEADER = {"type": "session", "version": 3, "id": "cc-1", "timestamp": "2026-10-30T10:00:00Z"}


def cmd_entry(entry_id, parent, role, content, meta=None, timestamp="2026-10-30T10:00:01Z"):
    message = {"role": role, "content": content, "meta": {"messageId": entry_id, "source": "user" if role == "user" else "model"}}
    if meta:
        message["meta"].update(meta)
    return {"type": "message", "id": entry_id, "parentId": parent, "timestamp": timestamp, "message": message}


def agy_record(kind, step, content="", source="MODEL", status="DONE", **extra):
    record = {"type": kind, "created_at": "2026-10-30T10:00:00Z", "source": source,
              "status": status, "step_index": step, "content": content}
    record.update(extra)
    return record


class NewHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.project_id = project_identity(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def cmd_session(self, records, session="cc-1", meta=None, cwd=None):
        directory = self.root / "cmd" / "users-josh-project"
        directory.mkdir(parents=True, exist_ok=True)
        header = dict(CMD_HEADER, id=session, cwd=cwd or str(self.project))
        path = directory / (session + ".jsonl")
        path.write_bytes(line(header) + b"".join(line(record) for record in records))
        if meta is not None:
            (directory / (session + ".meta.json")).write_text(json.dumps(meta))
        return directory, path

    def agy_conversation(self, conversation, records, workspaces=None, parent=None, step_count=None, full=True, chunks=False):
        base = self.root / "agy"
        logs = base / "brain" / conversation / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        payload = b"".join(line(record) for record in records)
        (logs / "transcript_full.jsonl").write_bytes(payload)
        if not full:
            (logs / "transcript_full.jsonl").unlink()
        (logs / "transcript.jsonl").write_bytes(payload)
        if chunks:
            chunk_dir = logs / "chunks" / "transcript_full"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            (chunk_dir / "00000000.jsonl").write_bytes(payload)
        database = base / "conversation_summaries.db"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversation_summaries (conversation_id TEXT PRIMARY KEY, workspace_uris TEXT, "
                       "parent_conversation_id TEXT, nesting_depth INTEGER, step_count INTEGER)")
            db.execute("INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                       (conversation, json.dumps([value for value in (workspaces or [])]), parent,
                        1 if parent else 0, step_count if step_count is not None else len(records)))
        return base

    def test_cmd_normalizes_blocks_tool_ids_and_keeps_unknown_entries(self):
        records = [
            cmd_entry("m1", None, "user", [{"type": "text", "text": "run the tests"}]),
            cmd_entry("m2", "m1", "assistant", [
                {"type": "thinking", "thinking": "private", "signature": "s"},
                {"type": "text", "text": "running"},
                {"type": "tool_use", "id": "t1", "name": "Shell", "input": {"command": "cargo test"}},
            ]),
            cmd_entry("m3", "m2", "user", [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "exited with code 0"}]}]),
            {"type": "model_change", "id": "m4", "parentId": "m3", "timestamp": "2026-10-30T10:00:02Z", "model": "x"},
            {"type": "future_entry", "id": "m5", "parentId": "m4", "timestamp": "2026-10-30T10:00:03Z", "detail": "retained"},
        ]
        directory, _ = self.cmd_session(records)
        with Store(self.state) as store:
            stats = store.refresh({"cmd": [directory]}, project=self.project_id)
            self.assertEqual(stats["sessions_indexed"], 1)
            session = "cmd:cc-1"
            call = store.db.execute("SELECT kind, call_id, text FROM events WHERE session=? AND call_id='t1'", (session,)).fetchone()
            self.assertEqual((call["kind"], call["call_id"]), ("assistant", "t1"))
            self.assertIn("cargo test", call["text"])
            result = store.db.execute("SELECT outcome, text FROM events WHERE session=? AND kind='tool-output'", (session,)).fetchone()
            self.assertEqual(result["outcome"], "ok")
            self.assertIn("exited with code 0", result["text"])
            unknown = store.db.execute("SELECT kind FROM events WHERE session=? AND kind LIKE 'unknown:%'", (session,)).fetchone()
            self.assertEqual(unknown["kind"], "unknown:future_entry")
            self.assertEqual(store.coverage()[0]["harness"], "cmd")

    def test_cmd_tool_result_without_status_stays_unknown(self):
        records = [
            cmd_entry("m1", None, "user", [{"type": "text", "text": "go"}]),
            cmd_entry("m2", "m1", "assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
            cmd_entry("m3", "m2", "user", [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "file contents"}]}]),
        ]
        directory, _ = self.cmd_session(records)
        with Store(self.state) as store:
            store.refresh({"cmd": [directory]}, project=self.project_id)
            row = store.db.execute("SELECT outcome FROM events WHERE kind='tool-output'").fetchone()
            self.assertIsNone(row["outcome"])

    def test_cmd_fork_uses_branch_point_not_the_newest_line(self):
        records = [
            cmd_entry("m1", None, "user", [{"type": "text", "text": "start"}]),
            cmd_entry("m2", "m1", "assistant", [{"type": "text", "text": "abandoned failure"}]),
            cmd_entry("m3", "m2", "assistant", [{"type": "text", "text": "active success"}]),
        ]
        directory, _ = self.cmd_session(records, meta={"parentSessionId": "source", "branchPoint": "m2"})
        with Store(self.state) as store:
            store.refresh({"cmd": [directory]}, project=self.project_id)
            self.assertEqual(store.db.execute("SELECT count(*) FROM events WHERE session='cmd:cc-1'").fetchone()[0], 4)
            rows = [dict(row) for row in store.db.execute("SELECT position, text, entry, parent_entry FROM events WHERE session='cmd:cc-1' AND kind='assistant' ORDER BY position")]
            self.assertEqual(active_branch(rows, "m2")[-1]["text"], "abandoned failure")
            self.assertEqual(rows[-1]["text"], "active success")
            self.assertEqual(store.db.execute("SELECT parent FROM sessions WHERE id='cmd:cc-1'").fetchone()[0], "cmd:source")

    def test_agy_prefers_the_full_transcript_and_never_indexes_chunks(self):
        records = [
            agy_record("USER_INPUT", 0, "please investigate", source="USER_EXPLICIT"),
            agy_record("PLANNER_RESPONSE", 1, "looking", tool_calls=[{"name": "read_file", "args": {"path": "a"}}]),
            agy_record("GENERIC", 2, "result text", status="DONE"),
        ]
        base = self.agy_conversation("conv-1", records, workspaces=[str(self.project)], chunks=True)
        with Store(self.state) as store:
            stats = store.refresh({"agy": [base / "brain"]}, project=self.project_id)
            self.assertEqual(stats["sessions_indexed"], 1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            session = "agy:conv-1"
            kinds = [row[0] for row in store.db.execute("SELECT kind FROM events WHERE session=? ORDER BY position", (session,))]
            self.assertEqual(kinds, ["user", "assistant", "tool-output"])
            self.assertEqual(store.db.execute("SELECT cwd FROM sessions WHERE id=?", (session,)).fetchone()[0], str(self.project))

    def test_agy_maps_status_and_reports_truncation(self):
        records = [
            agy_record("USER_INPUT", 0, "go", source="USER_EXPLICIT"),
            agy_record("PLANNER_RESPONSE", 1, "", tool_calls=[{"name": "shell", "args": {}}]),
            agy_record("GENERIC", 2, "boom", status="ERROR"),
            agy_record("GENERIC", 3, "short", truncated_fields=["content"]),
            agy_record("CHECKPOINT", 4, "summary"),
        ]
        base = self.agy_conversation("conv-2", records, workspaces=[str(self.project)])
        with Store(self.state) as store:
            store.refresh({"agy": [base / "brain"]}, project=self.project_id)
            session = "agy:conv-2"
            self.assertEqual(store.db.execute("SELECT outcome FROM events WHERE session=? AND position=(SELECT min(position) FROM events WHERE session=? AND kind='tool-output')", (session, session)).fetchone()[0], "error")
            self.assertEqual(store.db.execute("SELECT synthetic_id FROM events WHERE session=? AND kind='tool-call'", (session,)).fetchone()[0], 1)
            self.assertEqual(store.db.execute("SELECT kind FROM events WHERE session=? AND kind='compacted'", (session,)).fetchone()[0], "compacted")
            self.assertEqual(store.db.execute("SELECT kind FROM index_issues WHERE session=? AND kind='truncated_content'", (session,)).fetchone()[0], "truncated_content")
            self.assertEqual(store.db.execute("SELECT coverage FROM sessions WHERE id=?", (session,)).fetchone()[0], "degraded")

    def test_agy_distinguishes_multiple_workspaces_and_subagent_lineage(self):
        records = [agy_record("USER_INPUT", 0, "go", source="USER_EXPLICIT")]
        base = self.agy_conversation("parent-1", records, workspaces=[str(self.project)])
        self.agy_conversation("child-1", records, workspaces=[str(self.project), "/other/checkout"], parent="parent-1")
        with Store(self.state) as store:
            store.refresh({"agy": [base / "brain"]}, project=None)
            row = store.db.execute("SELECT cwd, workspaces, parent FROM sessions WHERE id='agy:child-1'").fetchone()
            self.assertEqual(row["cwd"], "")
            self.assertEqual(json.loads(row["workspaces"]), [str(self.project), "/other/checkout"])
            self.assertEqual(row["parent"], "agy:parent-1")

    def test_agy_without_summary_database_reports_unknown_ancestry(self):
        base = self.root / "agy-missing"
        logs = base / "brain" / "conv-3" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript.jsonl").write_bytes(line(agy_record("USER_INPUT", 0, "go", source="USER_EXPLICIT")))
        with Store(self.state) as store:
            store.refresh({"agy": [base / "brain"]}, project=None)
            row = store.db.execute("SELECT cwd, ancestry, source FROM sessions WHERE id='agy:conv-3'").fetchone()
            self.assertEqual((row["cwd"], row["ancestry"], row["source"]), ("", "unknown", None))

    def test_abandoned_branch_failure_is_invisible_to_experience_capture(self):
        from agent_memory.learning import Learner

        records = [
            cmd_entry("m1", None, "user", [{"type": "text", "text": "start"}]),
            cmd_entry("m3", "m1", "assistant", [{"type": "text", "text": "active success"}]),
            cmd_entry("m2", "m1", "assistant", [{"type": "text", "text": "abandoned failure"}]),
        ]
        directory, _ = self.cmd_session(records, meta={"branchPoint": "m3"})
        with Store(self.state) as store:
            store.refresh({"cmd": [directory]}, project=self.project_id)
            learner = Learner(store)
            events, _truncated = learner._events("cmd:cc-1")
            texts = [event["text"] for event in events if event["text"]]
            self.assertIn("active success", texts)
            self.assertNotIn("abandoned failure", texts)

    def test_harness_aliases_normalize_once(self):
        self.assertEqual(canonical_harness("command-code"), "cmd")
        self.assertEqual(canonical_harness("commandcode"), "cmd")
        self.assertEqual(canonical_harness("command"), "cmd")
        self.assertEqual(canonical_harness("antigravity"), "agy")
        self.assertEqual(canonical_harness("cmd"), "cmd")
        with self.assertRaises(ValueError):
            canonical_harness("nope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
