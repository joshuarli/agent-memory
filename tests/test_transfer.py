#!/usr/bin/env python3
import importlib.util
import json
import socket
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

ARCHIVE_SPEC = importlib.util.spec_from_file_location("agent_memory_archive", SRC_DIR / "agent_memory" / "archive.py")
ARCHIVE = importlib.util.module_from_spec(ARCHIVE_SPEC)
assert ARCHIVE_SPEC.loader is not None
sys.modules[ARCHIVE_SPEC.name] = ARCHIVE
ARCHIVE_SPEC.loader.exec_module(ARCHIVE)

TRANSFER_SPEC = importlib.util.spec_from_file_location("agent_memory_transfer", SRC_DIR / "agent_memory" / "transfer.py")
TRANSFER = importlib.util.module_from_spec(TRANSFER_SPEC)
assert TRANSFER_SPEC.loader is not None
sys.modules[TRANSFER_SPEC.name] = TRANSFER
TRANSFER_SPEC.loader.exec_module(TRANSFER)

LINE = ('{"timestamp":"2026-10-30T00:00:00Z","type":"response_item","payload":'
        '{"type":"message","role":"user","content":[{"type":"input_text","text":"%s"}]}}\n')


def line(text):
    return LINE % text


def manifest_for(chunks, native="x", artifacts=(), metadata=None, origin="peer"):
    size = sum(item[1] for item in chunks)
    offsets, total = [], 0
    for digest, length in chunks:
        offsets.append({"digest": digest, "size": length, "offset": total})
        total += length
    source_digest = ARCHIVE.source_digest_of(digest for digest, _ in chunks)
    listed = [{"name": name, "digest": digest, "size": length} for name, digest, length in artifacts]
    identity = ARCHIVE.manifest_digest(source_digest, size, [(name, digest) for name, digest, _ in artifacts], metadata)
    return {"type": "generation", "origin_host": origin, "harness": "codex", "native_id": native, "source_path": "/x",
            "source_digest": source_digest, "source_size": size, "captured_at": 1.0, "chunks": offsets,
            "artifacts": listed, "session_metadata": metadata, "manifest_digest": identity}


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.source_state = self.root / "source"
        self.central_state = self.root / "central"
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.project_id = project_identity(self.project)
        self.path = self.sessions / "rollout-a.jsonl"
        self.path.write_text(
            json.dumps({"timestamp": "2026-10-30T00:00:00Z", "type": "session_meta",
                        "payload": {"id": "a", "cwd": str(self.project)}}, separators=(",", ":")) + "\n"
            + line("alpha") + line("bravo", ))

    def tearDown(self):
        self.temp.cleanup()

    def capture(self):
        store = Store(self.source_state)
        store.refresh([self.sessions], project=self.project_id)
        return store

    def sync(self, source, remote="central", origin=None, label="test", attempts=3):
        origin = origin or ARCHIVE.origin_for(source.db)
        return TRANSFER.local_sync(source, self.central_state, remote, origin, label, attempts)

    def test_duplicate_delivery_is_idempotent_and_acks(self):
        source = self.capture()
        first = self.sync(source)
        self.assertEqual(len(first["delivered"]), 1)
        second = self.sync(source)
        self.assertEqual(second["delivered"], [])
        central = Store(self.central_state)
        self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 1)
        self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(central.db.execute("SELECT count(*) FROM events").fetchone()[0], 3)

    def test_dropped_acknowledgement_is_retried_and_duplicate_ingest_is_a_noop(self):
        source = self.capture()
        original = source.mark_durable

        def drop_once(generation, remote, remote_id):
            raise OSError("acknowledgement lost")

        source.mark_durable = drop_once
        with self.assertRaises(TRANSFER.TransferError):
            self.sync(source, attempts=1)
        source.mark_durable = original
        delivered = self.sync(source)
        self.assertEqual(len(delivered["delivered"]), 1)
        central = Store(self.central_state)
        self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 1)
        durable = source.db.execute("SELECT count(*) FROM transfer_generation WHERE durable_at IS NOT NULL").fetchone()[0]
        self.assertEqual(durable, 1)

    def test_corrupted_object_is_rejected_and_nothing_is_indexed(self):
        source = self.capture()
        original = source.read_object_bytes
        source.read_object_bytes = lambda digest: original(digest)[:-1] + b"X"
        with self.assertRaises(TRANSFER.TransferError):
            self.sync(source, attempts=1)
        central = Store(self.central_state)
        self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 0)
        self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)


    def serve_in_thread(self, allowed, exchange):
        left, right = socket.socketpair()
        left.settimeout(20)
        failure = {}

        def serve():
            try:
                with Store(self.central_state) as central:
                    with right.makefile("rb") as instream, right.makefile("wb") as outstream:
                        TRANSFER.receiver(central, instream, outstream, allowed_origins=allowed)
            except BaseException as exc:  # noqa: BLE001
                failure["error"] = exc

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with left.makefile("rb") as instream, left.makefile("wb") as outstream:
                return exchange(instream, outstream)
        finally:
            left.close()
            thread.join(timeout=20)

    def test_protocol_mismatch_is_refused_before_writing(self):
        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": 99, "origin": "x"})
            return TRANSFER.read_message(instream)

        reply = self.serve_in_thread(["x"], exchange)
        self.assertEqual(reply["type"], "error")

    def test_untrusted_origin_is_refused(self):
        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": TRANSFER.PROTOCOL, "origin": "stranger"})
            return TRANSFER.read_message(instream)

        reply = self.serve_in_thread(["allowed"], exchange)
        self.assertEqual(reply["type"], "error")
        self.assertIn("not allowed", reply["message"])

    def test_central_query_returns_verified_evidence_without_the_source(self):
        source = self.capture()
        self.sync(source)
        central = Store(self.central_state)
        self.path.unlink()
        rows = TRANSFER.handle_query(central, "search", {"query": "alpha"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"]["text"], "alpha")
        self.assertEqual(rows[0]["session"], "a@" + ARCHIVE.origin_for(source.db))
        self.assertEqual(rows[0]["origin_host"], ARCHIVE.origin_for(source.db))

    def test_tombstone_beats_a_replayed_generation(self):
        source = self.capture()
        self.sync(source)
        source.forget_session("codex:a", 2000.0)
        self.sync(source)
        central = Store(self.central_state)
        self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(central.db.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1)
        for row in source.db.execute("SELECT id FROM archive_generation").fetchall():
            source.db.execute("DELETE FROM archive_generation WHERE id = ?", (row["id"],))
        source.db.execute("DELETE FROM transfer_generation")
        source.db.execute("DELETE FROM tombstone")
        source.db.execute("DELETE FROM forgotten_sessions")
        source.db.commit()
        source.refresh([self.sessions], project=self.project_id)
        self.assertEqual(source.pending_transfers("central"), 1)
        with self.assertRaises(TRANSFER.TransferError):
            self.sync(source, attempts=1)
        central = Store(self.central_state)
        self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_same_native_id_from_two_origins_is_kept_separate(self):
        source = self.capture()
        origin_one = ARCHIVE.origin_for(source.db)
        self.sync(source, origin=origin_one)
        other_state = self.root / "other-source"
        other = Store(other_state)
        origin_two = ARCHIVE.ensure_identity(other.db, label="other", regenerate=True)[0]
        other.refresh([self.sessions], project=self.project_id)
        self.sync(other, origin=origin_two)
        central = Store(self.central_state)
        ids = sorted(row[0] for row in central.db.execute("SELECT id FROM sessions"))
        self.assertEqual(ids, sorted(["a@" + origin_one, "a@" + origin_two]))
        self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 2)

    def test_offline_backlog_persists_and_reports(self):
        source = self.capture()
        self.assertEqual(source.pending_transfers("central"), 1)
        with self.assertRaises((TRANSFER.TransferError, OSError)):
            tree = self.root / "not-a-store"
            tree.write_text("x")
            TRANSFER.local_sync(source, tree, "central", ARCHIVE.origin_for(source.db), "t", attempts=1)
        self.assertEqual(source.pending_transfers("central"), 1)
        attempt = source.db.execute("SELECT attempts, last_error FROM transfer_generation WHERE remote='central'").fetchone()
        self.assertGreaterEqual(attempt["attempts"], 1)
        self.assertTrue(attempt["last_error"])

    def test_forgetting_a_received_session_tombstones_its_own_origin(self):
        source = self.capture()
        self.sync(source)
        central = Store(self.central_state)
        peer = ARCHIVE.origin_for(source.db)
        session = central.db.execute("SELECT id FROM sessions").fetchone()[0]
        self.assertTrue(session.endswith("@" + peer))
        central.forget_session(session, 3000.0)
        self.assertTrue(central.is_tombstoned(peer, "codex", "a"))
        central.reindex_generations()
        self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        self.assertEqual(central.search("alpha", None), [])

    def corrupt_central_object(self):
        central = Store(self.central_state)
        digest = central.db.execute("SELECT digest FROM archive_generation_object LIMIT 1").fetchone()[0]
        object_path = ARCHIVE.object_path(central.archive_dir, digest)
        data = bytearray(object_path.read_bytes())
        data[0:1] = b"X"
        object_path.write_bytes(bytes(data))
        return central, digest

    def test_a_corrupt_central_object_is_never_acknowledged_as_durable(self):
        source = self.capture()
        self.sync(source)
        self.corrupt_central_object()
        with source.db:
            source.db.execute("DELETE FROM transfer_generation")
        original = source.read_object_bytes
        source.read_object_bytes = lambda requested: original(requested)[:-1] + b"X"
        with self.assertRaises(TRANSFER.TransferError):
            self.sync(source, attempts=1)
        durable = source.db.execute("SELECT count(*) FROM transfer_generation WHERE durable_at IS NOT NULL").fetchone()[0]
        self.assertEqual(durable, 0)

    def test_a_corrupt_central_object_is_repaired_from_the_sender_before_acknowledging(self):
        source = self.capture()
        self.sync(source)
        central, digest = self.corrupt_central_object()
        with source.db:
            source.db.execute("DELETE FROM transfer_generation")
        self.assertEqual(len(self.sync(source)["delivered"]), 1)
        self.assertEqual(central.read_object_bytes(digest), source.read_object_bytes(digest))

    def test_conflicting_sizes_for_one_digest_are_refused(self):
        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": TRANSFER.PROTOCOL, "origin": "peer"})
            TRANSFER.read_message(instream)
            manifest = manifest_for([("a" * 64, 1)])
            manifest["chunks"].append({"digest": "a" * 64, "size": 2, "offset": 1})
            manifest["source_size"] = 3
            TRANSFER.write_message(outstream, manifest)
            return TRANSFER.read_message(instream)

        reply = self.serve_in_thread(["peer"], exchange)
        self.assertEqual(reply["type"], "error")
        self.assertIn("conflicting", reply["message"])

    def cmd_source(self):
        directory = self.root / "cmd" / "slug"
        directory.mkdir(parents=True)
        (directory / "s-1.jsonl").write_text(cmd_header("s-1", self.project) + cmd_message("m-a", None, "alpha")
                                             + cmd_message("m-b", "m-a", "bravo"))
        meta = directory / "s-1.meta.json"
        meta.write_text(json.dumps({"branchPoint": "m-b"}))
        return directory, meta

    def test_a_changed_companion_reaches_central_as_a_new_generation(self):
        directory, meta = self.cmd_source()
        source = Store(self.source_state)
        roots = {"cmd": [self.root / "cmd"]}
        source.refresh(roots, project=None)
        origin = ARCHIVE.origin_for(source.db)
        endpoint = self.sync(source)["endpoint"]
        first = source.db.execute("SELECT id FROM archive_generation").fetchone()[0]
        meta.write_text(json.dumps({"branchPoint": "m-a"}))
        stats = source.refresh(roots, project=None)
        self.assertEqual((stats["archived"], stats["archive_objects"], stats["archive_bytes"]), (1, 0, 0))
        self.assertEqual(source.db.execute("SELECT branch_tip FROM sessions").fetchone()[0], "m-a")
        generations = [row["id"] for row in source.db.execute("SELECT id FROM archive_generation ORDER BY id")]
        self.assertEqual(len(generations), 2)
        old_artifacts = {row["name"]: row["digest"] for row in source.db.execute(
            "SELECT name, digest FROM archive_artifact WHERE generation = ?", (first,))}
        self.assertIn("s-1.meta.json", old_artifacts)
        self.assertEqual(ARCHIVE.sha256(json.dumps({"branchPoint": "m-b"}).encode()), old_artifacts["s-1.meta.json"])
        self.assertEqual(source.pending_transfers(endpoint), 1)
        self.assertEqual(len(self.sync(source)["delivered"]), 1)
        with Store(self.central_state) as central:
            self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 2)
            latest = central.db.execute("SELECT max(id) FROM archive_generation").fetchone()[0]
            stored = {row["name"]: row["digest"] for row in central.db.execute(
                "SELECT name, digest FROM archive_artifact WHERE generation = ?", (latest,))}
            self.assertEqual(central.read_object_bytes(stored["s-1.meta.json"]), json.dumps({"branchPoint": "m-a"}).encode())
            session = "cmd:s-1@" + origin
            self.assertEqual(central.db.execute("SELECT branch_tip FROM sessions WHERE id = ?", (session,)).fetchone()[0], "m-a")

    def send_raw(self, manifest):
        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": TRANSFER.PROTOCOL, "origin": "peer"})
            TRANSFER.read_message(instream)
            TRANSFER.write_message(outstream, manifest)
            return TRANSFER.read_message(instream)

        reply = self.serve_in_thread(["peer"], exchange)
        with Store(self.central_state) as central:
            self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 0)
            self.assertEqual(central.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        return reply

    def test_a_nonempty_source_with_no_chunks_is_refused_without_a_session(self):
        manifest = manifest_for([("c" * 64, 5)])
        manifest["chunks"] = []
        reply = self.send_raw(manifest)
        self.assertEqual(reply["type"], "error")
        self.assertIn("total 0 bytes", reply["message"])

    def test_a_gap_an_overlap_and_a_wrong_digest_are_each_refused(self):
        gap = manifest_for([("c" * 64, 5), ("d" * 64, 5)])
        gap["chunks"][1]["offset"] = 6
        overlap = manifest_for([("c" * 64, 5), ("d" * 64, 5)])
        overlap["chunks"][1]["offset"] = 4
        wrong = manifest_for([("c" * 64, 5)])
        wrong["source_digest"] = "e" * 64
        tampered = manifest_for([("c" * 64, 5)])
        tampered["session_metadata"] = {"cwd": "/x", "parent": None, "source": None, "thread_source": None,
                                        "ancestry": "known", "branch_tip": "m", "workspaces": None, "step_count": None}
        escaping = manifest_for([("c" * 64, 5)], artifacts=[("../escape", "d" * 64, 1)])
        for manifest, phrase in ((gap, "contiguous"), (overlap, "contiguous"), (wrong, "source digest"),
                                 (tampered, "manifest digest"), (escaping, "artifact name")):
            with self.subTest(phrase=phrase):
                reply = self.send_raw(manifest)
                self.assertEqual(reply["type"], "error")
                self.assertIn(phrase, reply["message"])

    def object_files(self, state):
        return sorted(path.name for path in (state / "archive" / "objects").rglob("*") if path.is_file() and not path.name.startswith("."))

    def test_a_received_forget_removes_evidence_and_withdraws_derived_records(self):
        from agent_memory.learning import Learner

        source = self.capture()
        self.sync(source)
        origin = ARCHIVE.origin_for(source.db)
        session = "a@" + origin
        with Store(self.central_state) as central:
            Learner(central)
            with central.db:
                for index, holder in ((1, session), (2, "kept@elsewhere")):
                    central.db.execute(
                        "INSERT INTO learning_experiences (project, kind, claim, applies_when, identifiers, fingerprint, basis, "
                        "detector, revision, worktree, observed_at, captured_at, validity, tags, state) "
                        "VALUES ('git:canary','gotcha','claim','cond','[]',?,'deterministic','det','r','w',10,10,'[]','','retained')",
                        ("fp%d" % index,))
                    central.db.execute(
                        "INSERT INTO learning_experience_sources (experience, session, position, digest, role) "
                        "VALUES (?, ?, 0, 'digest', 'trigger')", (index, holder))
                central.db.execute("INSERT INTO learning_observed (session, signature, observed_at, experiences) VALUES (?, 'sig', 1, 1)", (session,))
            self.assertNotEqual(self.object_files(self.central_state), [])
        source.forget_session("codex:a", 2000.0)
        self.sync(source)
        with Store(self.central_state) as central:
            for table in ("archive_generation", "archive_generation_object", "archive_artifact", "archive_object", "sessions", "events"):
                self.assertEqual(central.db.execute("SELECT count(*) FROM %s" % table).fetchone()[0], 0, table)
            self.assertEqual(self.object_files(self.central_state), [])
            self.assertEqual(central.search("alpha", None), [])
            self.assertEqual(central.db.execute("SELECT count(*) FROM events_fts WHERE events_fts MATCH 'alpha'").fetchone()[0], 0)
            self.assertEqual([row[0] for row in central.db.execute("SELECT id FROM learning_experiences")], [2])
            self.assertEqual(central.db.execute("SELECT count(*) FROM learning_experience_sources WHERE session = ?", (session,)).fetchone()[0], 0)
            self.assertEqual(central.db.execute("SELECT count(*) FROM learning_observed WHERE session = ?", (session,)).fetchone()[0], 0)
            self.assertEqual(central.db.execute("SELECT count(*) FROM tombstone").fetchone()[0], 1)

    def test_a_received_forget_keeps_objects_another_origin_still_references(self):
        source = self.capture()
        origin_one = ARCHIVE.origin_for(source.db)
        self.sync(source, origin=origin_one)
        other = Store(self.root / "other-source")
        origin_two = ARCHIVE.ensure_identity(other.db, label="other", regenerate=True)[0]
        other.refresh([self.sessions], project=self.project_id)
        self.sync(other, origin=origin_two)
        before = self.object_files(self.central_state)
        source.forget_session("codex:a", 2000.0)
        self.sync(source, origin=origin_one)
        with Store(self.central_state) as central:
            self.assertEqual(self.object_files(self.central_state), before)
            hits = central.search("alpha", None)
            self.assertEqual([hit["origin_host"] for hit in hits], [origin_two])
            self.assertEqual(central.resolve_evidence(hits[0]["session"], hits[0]["position"], hits[0]["digest"])["text"], "alpha")

    def seed_learning(self, source):
        from agent_memory.learning import Learner

        Learner(source)
        columns = ("project, kind, claim, applies_when, identifiers, fingerprint, basis, detector, revision, worktree, "
                   "observed_at, captured_at, validity, tags, state")
        with source.db:
            for index, (state, session) in enumerate((("retained", "a"), ("retained", "a"), ("retained", "foreign@elsewhere")), start=1):
                source.db.execute(
                    "INSERT INTO learning_experiences (%s) VALUES ('git:canary','gotcha',?,'when','[]',?,'deterministic','det','r','w',?,?,'[]','',?)" % columns,
                    ("claim %d" % index, "fp%d" % index, 10 + index, 10 + index, state))
                source.db.execute("INSERT INTO learning_experience_sources (experience, session, position, digest, role) VALUES (?, ?, 0, 'dg', 'trigger')",
                                  (index, session))
            source.db.execute("INSERT INTO learning_experience_links (newer, older, relation, created_at) VALUES (2, 1, 'supersedes', 1)")
            source.db.execute("INSERT INTO learning_findings (id, project, signal, fingerprint, terms, state, created_at, updated_at) "
                              "VALUES (7, 'git:canary', 'regression', 'ffp', 't', 'memory', 1, 1)")
            source.db.execute("INSERT INTO learning_incidents (finding, session, root_session, position, digest, signal, observed_at) "
                              "VALUES (7, 'a', 'a', 0, 'dg', 'regression', 5)")
            source.db.execute("INSERT INTO learning_memories (finding, project, action, applicability, revision, state, created_at, validity) "
                              "VALUES (7, 'git:canary', 'do the thing', 'when it applies', 'r', 'approved', 6, '[]')")

    def test_learning_records_sync_with_ids_provenance_lifecycle_and_advisory_authority(self):
        source = self.capture()
        self.seed_learning(source)
        stats = self.sync(source)
        origin = ARCHIVE.origin_for(source.db)
        self.assertEqual(stats["learning_records"], 3)
        with Store(self.central_state) as central:
            self.assertEqual(central.db.execute("SELECT remote_id, state FROM learning_import_experience ORDER BY remote_id").fetchall()[0][:], (1, "retained"))
            self.assertEqual([row[0] for row in central.db.execute("SELECT remote_id FROM learning_import_experience ORDER BY remote_id")], [1, 2])
            self.assertEqual(central.db.execute("SELECT count(*) FROM learning_experiences").fetchone()[0] if
                             central.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'learning_experiences'").fetchone() else 0, 0)
            sources = {(row["record"], row["remote_id"]): row["session"] for row in central.db.execute("SELECT * FROM learning_import_source")}
            self.assertEqual(sources[("experience", 1)], "a@" + origin)
            self.assertEqual(sources[("memory", 7)], "a@" + origin)
            self.assertEqual(central.db.execute("SELECT relation FROM learning_import_link WHERE newer = 2 AND older = 1").fetchone()[0], "supersedes")
            rows = TRANSFER.handle_query(central, "memory", {"limit": 10})
            self.assertEqual(sorted(row["record"] for row in rows), ["experience:1@" + origin, "experience:2@" + origin, "memory:7@" + origin])
            self.assertTrue(all("not an instruction" in row["authority"] for row in rows))
            self.assertTrue(all(row["origin_host"] == origin for row in rows))
            hit = central.search("alpha", None)[0]
            self.assertEqual(rows[0]["sources"][0]["session"], hit["session"])
        self.assertNotIn("learning_records", self.sync(source))
        with source.db:
            source.db.execute("UPDATE learning_experiences SET state = 'withdrawn', state_reason = 'withdrawn by user', state_at = 99 WHERE id = 1")
            source.db.execute("UPDATE learning_memories SET state = 'superseded' WHERE finding = 7")
            source.db.execute("DELETE FROM learning_experience_links")
            source.db.execute("DELETE FROM learning_experience_sources WHERE experience = 2")
            source.db.execute("DELETE FROM learning_experiences WHERE id = 2")
        self.assertEqual(self.sync(source)["learning_records"], 2)
        with Store(self.central_state) as central:
            states = {row["remote_id"]: (row["state"], row["state_reason"], row["state_at"]) for row in central.db.execute("SELECT * FROM learning_import_experience")}
            self.assertEqual(states, {1: ("withdrawn", "withdrawn by user", 99.0)})
            self.assertEqual(central.db.execute("SELECT state FROM learning_import_memory").fetchone()[0], "superseded")
            self.assertEqual(central.db.execute("SELECT count(*) FROM learning_import_link").fetchone()[0], 0)
            self.assertEqual(TRANSFER.handle_query(central, "memory", {"limit": 10}), [])

    def test_a_forgotten_session_deletes_imported_records_and_blocks_their_return(self):
        source = self.capture()
        self.seed_learning(source)
        self.sync(source)
        origin = ARCHIVE.origin_for(source.db)
        source.forget_session("a", 2000.0)
        self.sync(source)
        with Store(self.central_state) as central:
            for table in ("learning_import_experience", "learning_import_memory", "learning_import_source", "learning_import_link"):
                self.assertEqual(central.db.execute("SELECT count(*) FROM %s" % table).fetchone()[0], 0, table)
            central.db.execute("DELETE FROM learning_import_state")
            central.db.commit()
            records = TRANSFER.learning_sync.export_snapshot(source.db)
            self.assertEqual(TRANSFER.learning_sync.import_snapshot(central, origin, records, TRANSFER.learning_sync.snapshot_digest(records)), 0)

    def send_learning_frames(self, records, digest=None):
        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": TRANSFER.PROTOCOL, "origin": "peer"})
            TRANSFER.read_message(instream)
            snapshot_digest = digest or TRANSFER.learning_sync.snapshot_digest(records)
            TRANSFER.write_message(outstream, {"type": "learning_begin", "count": len(records), "digest": snapshot_digest})
            for record in records:
                TRANSFER.write_message(outstream, {"type": "learning_record", "record": record})
            TRANSFER.write_message(outstream, {"type": "learning_commit"})
            return TRANSFER.read_message(instream)

        return self.serve_in_thread(["peer"], exchange)

    def test_a_tombstone_that_lands_during_an_upload_refuses_the_commit_and_keeps_no_bytes(self):
        data = b'{"secret":"words"}\n'
        digest = ARCHIVE.sha256(data)

        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": TRANSFER.PROTOCOL, "origin": "peer"})
            TRANSFER.read_message(instream)
            TRANSFER.write_message(outstream, manifest_for([(digest, len(data))], native="racing"))
            self.assertEqual(TRANSFER.read_message(instream)["digests"], [digest])
            with Store(self.central_state) as other:
                other.apply_tombstone("peer", "codex", "racing", "global", 5.0)
            TRANSFER.write_message(outstream, {"type": "object", "digest": digest, "encoding": "identity"})
            TRANSFER.write_blob(outstream, data)
            TRANSFER.write_message(outstream, {"type": "commit"})
            return TRANSFER.read_message(instream)

        reply = self.serve_in_thread(["peer"], exchange)
        self.assertEqual(reply["type"], "error")
        self.assertIn("forgotten", reply["message"])
        with Store(self.central_state) as central:
            self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation").fetchone()[0], 0)
            self.assertEqual(central.db.execute("SELECT count(*) FROM archive_object").fetchone()[0], 0)
            self.assertEqual(self.object_files(self.central_state), [])

    def test_a_tombstone_applied_while_a_learning_snapshot_imports_still_removes_its_records(self):
        record = {"record": "memory", "id": 1, "project": "p", "action": "a", "applicability": "b", "revision": None,
                  "validity": "[]", "state": "approved", "created_at": 1.0, "links": [],
                  "sources": [{"session": "cmd:racing", "position": 0, "digest": "dg", "role": "regression"}]}
        digest = TRANSFER.learning_sync.snapshot_digest([record])
        racer = {}
        with Store(self.central_state) as central:
            original = TRANSFER.learning_sync._tombstoned

            def tombstone_arrives_after_the_check(store, origin, rec):
                answer = original(store, origin, rec)

                def forget():
                    with Store(self.central_state) as other:
                        TRANSFER.learning_sync.forget_session_imports(other.db, "cmd:racing@peer")
                        other.apply_tombstone("peer", "cmd", "racing", "global", 5.0)

                racer["thread"] = threading.Thread(target=forget)
                racer["thread"].start()
                time.sleep(0.3)
                return answer

            with mock.patch.object(TRANSFER.learning_sync, "_tombstoned", tombstone_arrives_after_the_check):
                TRANSFER.learning_sync.import_snapshot(central, "peer", [record], digest)
            racer["thread"].join(10)
            central.db.rollback()
            self.assertEqual(central.db.execute("SELECT count(*) FROM learning_import_memory").fetchone()[0], 1)
            with central.db:
                TRANSFER.learning_sync.forget_session_imports(central.db, "cmd:racing@peer")
            self.assertEqual(central.db.execute("SELECT count(*) FROM learning_import_memory").fetchone()[0], 0)

    def test_a_record_longer_than_the_central_read_limit_is_indexed_as_an_issue_not_a_failure(self):
        big = self.sessions / "rollout-big.jsonl"
        big.write_text(
            json.dumps({"timestamp": "2026-10-30T00:00:00Z", "type": "session_meta", "payload": {"id": "big", "cwd": str(self.project)}}) + "\n"
            + line("x" * (9 * 1024 * 1024)) + line("after the long line"))
        source = self.capture()
        stats = self.sync(source)
        self.assertEqual(len(stats["delivered"]), 2)
        with Store(self.central_state) as central:
            session = "big@" + ARCHIVE.origin_for(source.db)
            self.assertEqual(central.db.execute("SELECT count(*) FROM archive_generation WHERE indexed_at IS NULL").fetchone()[0], 0)
            kinds = [row[0] for row in central.db.execute("SELECT kind FROM index_issues WHERE session = ?", (session,))]
            self.assertEqual(kinds, ["oversized_record"])
            self.assertEqual(len(central.search("after the long line", None)), 1)
            self.assertEqual(central.reindex_generations(), 2)

    def test_malformed_or_mismatched_learning_snapshots_are_refused_without_changes(self):
        good = {"record": "memory", "id": 1, "project": "p", "action": "a", "applicability": "b", "revision": None,
                "validity": "[]", "state": "approved", "created_at": 1.0, "sources": [], "links": []}
        bad_state = {**good, "state": "promoted"}
        unknown = {**good, "record": "instruction"}
        for records, digest, phrase in (([bad_state], None, "unsupported state"), ([unknown], None, "kind"),
                                        ([good], "0" * 64, "digest"), ([good, good], None, "repeats")):
            with self.subTest(phrase=phrase):
                reply = self.send_learning_frames(records, digest)
                self.assertEqual(reply["type"], "error")
                self.assertIn(phrase, reply["message"])
                with Store(self.central_state) as central:
                    tables = {row[0] for row in central.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    if "learning_import_memory" in tables:
                        self.assertEqual(central.db.execute("SELECT count(*) FROM learning_import_memory").fetchone()[0], 0)
        reply = self.send_learning_frames([good])
        self.assertEqual(reply["type"], "learning_ack")
        with Store(self.central_state) as central:
            self.assertEqual(central.db.execute("SELECT origin_host, remote_id FROM learning_import_memory").fetchall()[0][:], ("peer", 1))

    def test_central_memory_records_carry_origin_evidence_and_no_authority(self):
        from agent_memory.learning import Learner

        source = self.capture()
        self.sync(source)
        central = Store(self.central_state)
        Learner(central)
        with central.db:
            central.db.execute(
                "INSERT INTO learning_experiences (project, kind, claim, applies_when, identifiers, fingerprint, basis, "
                "detector, revision, worktree, observed_at, captured_at, validity, tags, state) "
                "VALUES ('git:canary','gotcha','imported claim','cond','[]','fp','deterministic','det','r','w',10,10,'[]','','retained')")
            central.db.execute(
                "INSERT INTO learning_experience_sources (experience, session, position, digest, role) "
                "VALUES (1, 'a@peer', 0, 'digest', 'trigger')")
        rows = TRANSFER.handle_query(central, "memory", {"limit": 5})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record"], "experience:1")
        self.assertIn("not an instruction", rows[0]["authority"])
        self.assertEqual(rows[0]["sources"][0]["session"], "a@peer")

    def test_objects_are_compressed_and_still_verify(self):
        source = self.capture()
        self.sync(source)
        central = Store(self.central_state)
        generation = central.db.execute("SELECT id FROM archive_generation").fetchone()[0]
        self.assertEqual(ARCHIVE.verify_generation(central.db, central.archive_dir, generation), 1)
        rows = TRANSFER.handle_query(central, "search", {"query": "alpha"})
        self.assertEqual(rows[0]["evidence"]["text"], "alpha")
        original = b"alpha\n" * 4096
        packed = TRANSFER.compress(original)
        self.assertLess(len(packed), len(original))
        self.assertEqual(TRANSFER.decompress(packed, len(original)), original)

    def test_a_compressed_object_that_inflates_past_its_declared_size_is_refused(self):
        payload = TRANSFER.compress(b"x" * 100000)

        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": TRANSFER.PROTOCOL, "origin": "peer",
                                               "compression": ["zlib"]})
            ready = TRANSFER.read_message(instream)
            self.assertEqual(ready["compression"], "zlib")
            TRANSFER.write_message(outstream, manifest_for([("a" * 64, 4)], native="bomb"))
            need = TRANSFER.read_message(instream)
            self.assertEqual(need["digests"], ["a" * 64])
            TRANSFER.write_message(outstream, {"type": "object", "digest": "a" * 64, "encoding": "zlib"})
            TRANSFER.write_blob(outstream, payload)
            return TRANSFER.read_message(instream)

        reply = self.serve_in_thread(["peer"], exchange)
        self.assertEqual(reply["type"], "error")
        self.assertIn("declared size", reply["message"])

    def test_a_corrupt_compressed_payload_is_refused(self):
        def exchange(instream, outstream):
            TRANSFER.write_message(outstream, {"type": "hello", "protocol": TRANSFER.PROTOCOL, "origin": "peer",
                                               "compression": ["zlib"]})
            TRANSFER.read_message(instream)
            TRANSFER.write_message(outstream, manifest_for([("b" * 64, 4)], native="corrupt"))
            TRANSFER.read_message(instream)
            TRANSFER.write_message(outstream, {"type": "object", "digest": "b" * 64, "encoding": "zlib"})
            TRANSFER.write_blob(outstream, b"not zlib at all")
            return TRANSFER.read_message(instream)

        reply = self.serve_in_thread(["peer"], exchange)
        self.assertEqual(reply["type"], "error")
        self.assertIn("decompress", reply["message"])


def cmd_message(identifier, parent, text):
    return json.dumps({"type": "message", "id": identifier, "parentId": parent, "timestamp": "2026-10-30T00:00:00Z",
                       "message": {"role": "user", "content": [{"type": "text", "text": text}],
                                   "meta": {"messageId": identifier}}}, separators=(",", ":")) + "\n"


def cmd_header(identifier, cwd):
    return json.dumps({"type": "session", "version": 3, "id": identifier, "timestamp": "2026-10-30T00:00:00Z",
                       "cwd": str(cwd)}, separators=(",", ":")) + "\n"


def agy_step(text):
    return json.dumps({"type": "USER_INPUT", "created_at": "2026-10-30T00:00:00Z", "source": "USER_EXPLICIT",
                       "status": "DONE", "step_index": 0, "content": text}, separators=(",", ":")) + "\n"


class CentralParityTests(unittest.TestCase):
    """A session indexed centrally from captured bytes must equal the locally indexed one."""

    SESSION_COLUMNS = ("harness", "native_id", "cwd", "source", "thread_source", "ancestry", "branch_tip", "workspaces",
                       "step_count", "coverage", "recognized_events", "unrecognized_events", "malformed_lines",
                       "partial_bytes", "indexed_bytes")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.first = self.root / "first"
        self.second = self.root / "second"
        self.first.mkdir()
        self.second.mkdir()
        self.cmd_dir = self.root / "cmd" / "slug"
        self.cmd_dir.mkdir(parents=True)
        self.claude_dir = self.root / "claude" / "proj"
        (self.claude_dir / "main" / "subagents").mkdir(parents=True)
        self.agy_root = self.root / "agy"
        logs = self.agy_root / "brain"
        database = self.agy_root / "conversation_summaries.db"
        with sqlite3.connect(self._prepare(database)) as db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, workspace_uris TEXT, "
                       "parent_conversation_id TEXT, nesting_depth INTEGER, step_count INTEGER)")
            db.execute("INSERT INTO conversation_summaries VALUES ('conv-multi', ?, NULL, 0, 1)",
                       (json.dumps(["file://%s" % self.first, "file://%s" % self.second]),))
            db.execute("INSERT INTO conversation_summaries VALUES ('conv-child', ?, 'conv-multi', 1, 5)",
                       (json.dumps(["file://%s" % self.first]),))
        for conversation in ("conv-multi", "conv-child"):
            directory = logs / conversation / ".system_generated" / "logs"
            directory.mkdir(parents=True)
            (directory / "transcript_full.jsonl").write_text(agy_step("agy " + conversation))
            (directory / "transcript.jsonl").write_text(agy_step("agy " + conversation))
        (self.cmd_dir / "s-1.jsonl").write_text(
            cmd_header("s-1", self.first) + cmd_message("m-a", None, "alpha") + cmd_message("m-b", "m-a", "bravo")
            + cmd_message("m-c", "m-b", "charlie") + "{not json}\n" + '{"type":"message","id":"partial"')
        (self.cmd_dir / "s-1.meta.json").write_text(json.dumps({"branchPoint": "m-b"}))
        (self.cmd_dir / "s-2.jsonl").write_text(cmd_header("s-2", self.second) + cmd_message("n-a", None, "delta"))
        (self.cmd_dir / "s-2.meta.json").write_text(json.dumps({"parentSessionId": "s-1"}))
        main = {"sessionId": "main", "cwd": str(self.first), "isSidechain": False}
        (self.claude_dir / "main.jsonl").write_text(json.dumps({**main, "type": "user", "message": {"role": "user", "content": "claude parent"}}) + "\n")
        (self.claude_dir / "main" / "subagents" / "agent-a1.jsonl").write_text(
            json.dumps({**main, "isSidechain": True, "type": "user", "message": {"role": "user", "content": "claude child"}}) + "\n")
        self.roots = {"cmd": [self.root / "cmd"], "claude": [self.root / "claude"], "agy": [self.agy_root]}

    @staticmethod
    def _prepare(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def tearDown(self):
        self.temp.cleanup()

    def normalized(self, db, origin_suffix=""):
        sessions = {}
        for row in db.execute("SELECT * FROM sessions ORDER BY id"):
            key = (row["harness"], row["native_id"])
            parent = row["parent"]
            if parent and origin_suffix:
                self.assertTrue(parent.endswith(origin_suffix), parent)
                parent = parent[:-len(origin_suffix)]
            events = [tuple(event) for event in db.execute(
                "SELECT position, kind, text, digest, outcome, entry, parent_entry, synthetic_id FROM events "
                "WHERE session = ? ORDER BY position", (row["id"],))]
            issues = [tuple(issue) for issue in db.execute(
                "SELECT position, kind FROM index_issues WHERE session = ? ORDER BY position, kind", (row["id"],))]
            sessions[key] = ({column: row[column] for column in self.SESSION_COLUMNS}, parent, events, issues)
        return sessions

    def test_central_sessions_equal_local_sessions_for_forks_rewinds_subagents_and_workspaces(self):
        source = Store(self.root / "source")
        source.refresh(self.roots, project=None)
        origin = ARCHIVE.origin_for(source.db)
        TRANSFER.local_sync(source, self.root / "central", "central", origin, "source")
        local = self.normalized(source.db)
        with Store(self.root / "central") as central:
            remote = self.normalized(central.db, "@" + origin)
        self.assertEqual(set(local), set(remote))
        self.assertEqual(len(local), 6)
        for key in local:
            self.assertEqual(local[key], remote[key], key)
        self.assertEqual(local[("cmd", "s-1")][0]["branch_tip"], "m-b")
        self.assertEqual(local[("cmd", "s-2")][1], "cmd:s-1")
        self.assertEqual(local[("agy", "conv-child")][1], "agy:conv-multi")
        self.assertEqual(local[("agy", "conv-child")][0]["coverage"], "degraded")
        self.assertEqual(json.loads(local[("agy", "conv-multi")][0]["workspaces"]), [str(self.first), str(self.second)])
        self.assertEqual(local[("claude", "agent-a1")][0]["thread_source"], "subagent")

    def test_central_branch_selection_follows_the_captured_branch_tip(self):
        source = Store(self.root / "source")
        source.refresh(self.roots, project=None)
        origin = ARCHIVE.origin_for(source.db)
        TRANSFER.local_sync(source, self.root / "central", "central", origin, "source")
        with Store(self.root / "central") as central:
            tip = central.db.execute("SELECT branch_tip FROM sessions WHERE id = ?", ("cmd:s-1@" + origin,)).fetchone()[0]
            events = [dict(row) for row in central.db.execute(
                "SELECT position, entry, parent_entry FROM events WHERE session = ?", ("cmd:s-1@" + origin,))]
        active = STORE_MODULE.active_branch(events, tip)
        self.assertEqual([event["entry"] for event in active if event["entry"]], ["m-a", "m-b"])


SILENT_AFTER_HELLO = (
    "import json, struct, sys, time\n"
    "head = sys.stdin.buffer.read(8)\n"
    "sys.stdin.buffer.read(struct.unpack('>Q', head)[0])\n"
    "body = json.dumps({'type': 'ready', 'protocol': %d, 'compression': None, 'store': 'silent-store'}).encode()\n"
    "sys.stdout.buffer.write(struct.pack('>Q', len(body)) + body)\n"
    "sys.stdout.buffer.flush()\n"
    "time.sleep(120)\n"
)
NEVER_ANSWERS = "import time\ntime.sleep(120)\n"


class SshTransportTests(unittest.TestCase):
    """A connected but silent receiver must not hang a query or a sync."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.root = Path(self.temp.name)
        self.processes = []
        real = subprocess.Popen

        def recording(*args, **kwargs):
            process = real(*args, **kwargs)
            self.processes.append(process)
            return process

        self.patches = [
            mock.patch.object(TRANSFER.subprocess, "Popen", recording),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.temp.cleanup()

    def receiver_script(self, script):
        return mock.patch.object(TRANSFER, "ssh_argv", lambda host, command: [sys.executable, "-I", "-c", script])

    def assert_reaped(self):
        receivers = [process for process in self.processes if process.args[0] == sys.executable]
        self.assertEqual(len(receivers), 1)
        self.assertIsNotNone(receivers[0].poll())

    def test_a_query_to_a_receiver_that_never_answers_times_out_and_reaps_the_process(self):
        started = time.monotonic()
        with self.receiver_script(NEVER_ANSWERS):
            with self.assertRaises(TRANSFER.TransferError) as caught:
                TRANSFER.query_over_ssh("host", "cmd", "search", {"query": "x"}, "origin", "label", timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("without activity", str(caught.exception))
        self.assert_reaped()

    def test_a_query_to_a_receiver_that_goes_silent_after_the_handshake_times_out(self):
        started = time.monotonic()
        with self.receiver_script(SILENT_AFTER_HELLO % TRANSFER.PROTOCOL):
            with self.assertRaises(TRANSFER.TransferError) as caught:
                TRANSFER.query_over_ssh("host", "cmd", "search", {"query": "x"}, "origin", "label", timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("without activity", str(caught.exception))
        self.assert_reaped()

    def test_a_sync_to_a_receiver_that_goes_silent_keeps_the_backlog_and_reaps_the_process(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        (sessions / "rollout-a.jsonl").write_text(
            json.dumps({"timestamp": "2026-10-30T00:00:00Z", "type": "session_meta", "payload": {"id": "a", "cwd": str(self.root)}}) + "\n"
            + line("alpha"))
        source = Store(self.root / "source")
        source.refresh([sessions], project=None)
        origin = ARCHIVE.origin_for(source.db)
        started = time.monotonic()
        with self.receiver_script(SILENT_AFTER_HELLO % TRANSFER.PROTOCOL), mock.patch.object(TRANSFER.time, "sleep"):
            with self.assertRaises(TRANSFER.TransferError) as caught:
                TRANSFER.ssh_sender(source, "central", "host", "cmd", origin, "label", timeout=1)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("without activity", str(caught.exception))
        self.assertEqual(source.pending_transfers("silent-store"), 1)
        self.assert_reaped()


if __name__ == "__main__":
    unittest.main(verbosity=2)
