#!/usr/bin/env python3
import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "tests"))
import testkit  # noqa: E402

SRC = testkit.SRC
sys.path.insert(0, str(SRC))

from agent_memory import archive as archive_module  # noqa: E402
from agent_memory import backup as backup_module  # noqa: E402
from agent_memory import experience as experience_module  # noqa: E402
from agent_memory import learning as learning_module  # noqa: E402
from agent_memory import recall as recall_module  # noqa: E402
from agent_memory import retention  # noqa: E402
from agent_memory import store as store_module  # noqa: E402
from agent_memory.sessions_cli import main as sessions_main  # noqa: E402

Store = store_module.Store
project_identity = store_module.project_identity

BASE = 1_700_000_000.0
DAY = 86400.0


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP), prefix="retention-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("retention fixture\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Synthetic Agent",
                        "-c", "user.email=synthetic@example.invalid", "commit", "-q", "-m", "initial"], check=True)
        self.ident = project_identity(self.repo)
        self.store = Store(self.state)
        self.addCleanup(self.close_store)
        learning_module.initialize(self.store.db)

    def close_store(self):
        try:
            self.store.db.close()
        except Exception:
            pass

    def write_session(self, name, turns, age_days=1.0):
        path = self.sessions / ("rollout-" + name + ".jsonl")
        stamp = BASE - age_days * DAY
        when = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [json.dumps({"timestamp": when, "type": "session_meta",
                            "payload": {"id": name, "cwd": str(self.repo)}}, separators=(",", ":"))]
        rows.extend(self.message(role, text, when) for role, text in turns)
        path.write_text("\n".join(rows) + "\n")
        os.utime(path, (stamp, stamp))
        return path

    @staticmethod
    def message(role, text, when):
        return json.dumps({"timestamp": when, "type": "response_item",
                           "payload": {"type": "message", "role": role,
                                       "content": [{"type": "input_text" if role == "user" else "output_text",
                                                    "text": text}]}}, separators=(",", ":"))

    def append_session(self, path, role, text, stamp):
        when = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("a") as stream:
            stream.write(self.message(role, text, when) + "\n")
        os.utime(path, (stamp, stamp))

    def refresh(self):
        return self.store.refresh(roots=[self.sessions], project=self.ident)

    def event(self, needle):
        row = self.store.db.execute(
            "SELECT session, position, digest, text FROM events WHERE text LIKE ? ORDER BY position LIMIT 1",
            ("%" + needle + "%",)).fetchone()
        self.assertIsNotNone(row, "no event contains %r" % (needle,))
        return row

    def add_experience(self, name, *, session=None, claim=None, applies_when="Synthetic retention path.",
                       kind="method", detector="synthetic", subject=None, observed_at=None, now=None, sources=None):
        now = BASE if now is None else now
        claim = claim or ("Synthetic experience %s keeps unique token zz%s." % (name, name))
        session = session or ("codex:" + name)
        draft = experience_module.Draft(
            kind=kind, claim=claim, applies_when=applies_when, detector=detector,
            subject=subject or name, observed_at=now if observed_at is None else observed_at,
            sources=sources or [{"session": session, "position": 0, "digest": name[0] * 64, "role": "trigger"}])
        with self.store.db:
            status, experience_id = experience_module.add_experience(
                self.store.db, self.ident, draft, session, now)
        self.assertIn(status, ("created", "merged"))
        return int(experience_id)

    def state_of(self, experience_id):
        return self.store.db.execute(
            "SELECT state, state_reason FROM learning_experiences WHERE id = ?", (experience_id,)).fetchone()

    def test_unused_single_session_experience_retires_after_180_days(self):
        experience_id = self.add_experience("unused")
        before = retention.plan(self.store, BASE + 179 * DAY)
        self.assertEqual([item["id"] for item in before["retire"]], [])
        after = retention.plan(self.store, BASE + 181 * DAY)
        listed = {item["id"]: item["claim"] for item in after["retire"]}
        self.assertIn(experience_id, listed)
        self.assertEqual(listed[experience_id], "Synthetic experience unused keeps unique token zzunused.")

    def test_rare_approved_memory_is_never_retired(self):
        with self.store.db:
            finding = self.store.db.execute(
                "INSERT INTO learning_findings(project, signal, fingerprint, terms, state, created_at, updated_at, proposal, integrated_at) "
                "VALUES (?, 'correction', 'retention-memory', 'rare', 'memory', ?, ?, '{}', ?)",
                (self.ident, BASE, BASE, BASE)).lastrowid
            self.store.db.execute(
                "INSERT INTO learning_memories(finding, project, action, applicability, revision, state, created_at) "
                "VALUES (?, ?, 'Keep this rare constraint.', 'Only in this fixture.', 'fixture', 'approved', ?)",
                (finding, self.ident, BASE))
        result = retention.plan(self.store, BASE + 365 * DAY)
        self.assertEqual(result["retire"], [])
        self.assertEqual(self.store.db.execute("SELECT state FROM learning_memories").fetchone()["state"], "approved")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM learning_memories").fetchone()[0], 1)

    def test_reobserved_retired_experience_is_retained_and_leaves_retire(self):
        experience_id = self.add_experience("reobserved", session="codex:first")
        self.assertIn(experience_id, [item["id"] for item in retention.plan(self.store, BASE + 181 * DAY)["retire"]])
        retired = experience_module.retire_stale(self.store.db, BASE + 181 * DAY)
        self.assertGreater(retired, 0)
        self.assertEqual(self.state_of(experience_id)["state"], "retired")
        self.add_experience("reobserved", session="codex:second", now=BASE + 182 * DAY)
        row = self.state_of(experience_id)
        self.assertEqual(row["state"], "retained")
        self.assertIn("codex:second", row["state_reason"])
        self.assertNotIn(experience_id, [item["id"] for item in retention.plan(self.store, BASE + 200 * DAY)["retire"]])

    def test_reobserved_withdrawn_experience_stays_withdrawn(self):
        experience_id = self.add_experience("withdrawn-reobservable", session="codex:first")
        with self.store.db:
            self.store.db.execute(
                "UPDATE learning_experiences SET state = 'withdrawn', state_reason = 'withdrawn by user', state_at = ? WHERE id = ?",
                (BASE, experience_id))
        self.add_experience("withdrawn-reobservable", session="codex:second", now=BASE + 2 * DAY)
        row = self.state_of(experience_id)
        self.assertEqual(row["state"], "withdrawn")
        self.assertEqual(row["state_reason"], "withdrawn by user")
        self.assertNotIn(experience_id, [item["id"] for item in retention.plan(self.store, BASE + 365 * DAY)["retire"]])

    def test_superseding_experience_wins_recall_and_old_source_event_is_historical(self):
        self.write_session("old-resolution", [
            ("user", "zzalpha_orbit_core is missing in pyproject.toml"),
            ("assistant", "Use `pip install -e .` to fix zzalpha_orbit_core in pyproject.toml."),
        ], age_days=30)
        self.write_session("new-resolution", [
            ("user", "zzalpha_orbit_core is missing in pyproject.toml"),
            ("assistant", "Use `make deps` to fix zzalpha_orbit_core in pyproject.toml."),
        ], age_days=5)
        self.refresh()
        old_event = self.event("pip install -e .")
        new_event = self.event("make deps")
        old_id = self.add_experience(
            "old-resolution", session=old_event["session"], detector="resolved_failure",
            claim="Use `pip install -e .` to fix zzalpha_orbit_core in pyproject.toml.",
            applies_when="When zzalpha_orbit_core is missing.", observed_at=BASE - 30 * DAY,
            sources=[{"session": old_event["session"], "position": old_event["position"],
                      "digest": old_event["digest"], "role": "trigger"}])
        new_id = self.add_experience(
            "new-resolution", session=new_event["session"], detector="resolved_failure",
            claim="Use `make deps` to fix zzalpha_orbit_core in pyproject.toml.",
            applies_when="When zzalpha_orbit_core is missing.", observed_at=BASE - 5 * DAY,
            sources=[{"session": new_event["session"], "position": new_event["position"],
                      "digest": new_event["digest"], "role": "trigger"}])
        self.assertEqual(self.state_of(old_id)["state"], "superseded")
        response = recall_module.Retriever(self.store, self.ident, self.repo, now=BASE).retrieve(
            "zzalpha_orbit_core", limit=10)
        refs = [item["ref"] for item in response["results"]]
        self.assertIn("experience:%d" % new_id, refs)
        self.assertNotIn("experience:%d" % old_id, refs)
        event_ref = "event:%s@%d" % (old_event["session"], old_event["position"])
        event = next(item for item in response["results"] if item["ref"] == event_ref)
        self.assertEqual(event["status"], "historical")
        self.assertIn("superseded_experience:experience:%d" % old_id, event["reasons"])
        self.assertEqual(retention.plan(self.store, BASE)["historical_events"], 1)

    def test_retained_evidence_survives_compaction_and_live_source_removal(self):
        path = self.write_session("compact", [
            ("user", "the zzcompact import fails"),
            ("assistant", "Run `make deps` to repair zzcompact."),
        ], age_days=3)
        self.refresh()
        event = self.event("make deps")
        experience_id = self.add_experience(
            "compact", session=event["session"], detector="resolved_failure",
            claim="Run `make deps` to repair zzcompact.", applies_when="When zzcompact import fails.",
            observed_at=BASE - 3 * DAY,
            sources=[{"session": event["session"], "position": event["position"],
                      "digest": event["digest"], "role": "trigger"}])
        result = archive_module.compact(self.store.db, self.store.archive_dir, apply=True)
        self.assertEqual(result["corrupt"], 0)
        path.unlink()
        sources = self.store.db.execute(
            "SELECT session, position, digest FROM learning_experience_sources WHERE experience = ?",
            (experience_id,)).fetchall()
        self.assertTrue(sources)
        for source in sources:
            record = self.store.resolve_evidence(source["session"], source["position"], source["digest"])
            self.assertEqual(record["digest"], source["digest"])

    def test_plan_keeps_latest_and_pinned_generations_and_counts_prunable_bytes(self):
        path = self.write_session("multi", [("user", "first zzmulti line")], age_days=1)
        self.refresh()
        self.append_session(path, "user", "second zzmulti line", BASE - 900)
        self.refresh()
        self.append_session(path, "user", "third zzmulti line", BASE - 800)
        self.refresh()
        generations = [int(row[0]) for row in self.store.db.execute("SELECT id FROM archive_generation ORDER BY id")]
        self.assertEqual(len(generations), 3)
        session_id = self.store.db.execute(
            "SELECT id FROM sessions WHERE native_id = 'multi'").fetchone()["id"]
        with self.store.db:
            self.store.db.execute("UPDATE sessions SET archive_generation = ? WHERE id = ?",
                                  (generations[0], session_id))
        self.add_experience(
            "multi", session=session_id,
            sources=[{"session": session_id, "position": 0, "digest": "a" * 64, "role": "trigger"}])
        plan = retention.plan(self.store, BASE)
        prunable = plan["generations"]["prunable_ids"]
        self.assertNotIn(generations[0], prunable)
        self.assertNotIn(generations[2], prunable)
        self.assertEqual(prunable, [generations[1]])
        sizes = {row["digest"]: int(row["size"]) for row in self.store.db.execute(
            "SELECT digest, size FROM archive_object")}
        kept = archive_module._generation_digests(self.store.db, generations[0])
        kept |= archive_module._generation_digests(self.store.db, generations[2])
        only_prunable = archive_module._generation_digests(self.store.db, generations[1]) - kept
        expected = sum(sizes[digest] for digest in only_prunable)
        self.assertGreater(expected, 0)
        self.assertEqual(plan["generations"]["prunable"], 1)
        self.assertEqual(plan["generations"]["prunable_bytes"], expected)

    def test_archive_plan_changes_no_database_bytes_and_no_archive_files(self):
        self.write_session("inspect", [("user", "archive plan must not write zzinspect")], age_days=2)
        self.refresh()
        self.add_experience("inspect")
        self.store.close()
        before = self.snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = sessions_main(["archive", "plan", "--state-dir", str(self.state), "--json"])
        self.assertEqual(code, 0)
        document = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(set(document), {"retire", "generations", "historical_events"})
        after = self.snapshot()
        self.assertEqual(before, after)

    def snapshot(self):
        database = (self.state / "sessions.sqlite3").read_bytes()
        archive_root = self.state / "archive"
        listing = sorted((str(path.relative_to(archive_root)), path.stat().st_size)
                         for path in archive_root.rglob("*") if path.is_file())
        return database, listing

    def test_forgotten_session_stays_gone_after_backup_restore(self):
        self.write_session("forgotten", [
            ("user", "zzforgotten unique token"),
            ("assistant", "The zzforgotten constraint is real."),
        ], age_days=4)
        self.refresh()
        event = self.event("zzforgotten")
        session_id = event["session"]
        self.add_experience(
            "forgotten", session=session_id, claim="Respect the zzforgotten constraint.",
            sources=[{"session": session_id, "position": event["position"],
                      "digest": event["digest"], "role": "trigger"}])
        backup_dir = self.root / "backup"
        backup_module.create(self.store, backup_dir)
        learner = learning_module.Learner(self.store, None)
        learner.forget_session(session_id, BASE + 1)
        self.assertIsNone(self.store.db.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone())
        restored_state = self.root / "restored"
        backup_module.restore(backup_dir, restored_state, str(self.state))
        restored = Store(restored_state)
        self.addCleanup(restored.db.close)
        self.assertIsNone(restored.db.execute(
            "SELECT 1 FROM sessions WHERE native_id = 'forgotten'").fetchone())
        self.assertEqual(restored.db.execute("SELECT count(*) FROM learning_experiences").fetchone()[0], 0)
        plan = retention.plan(restored, BASE + 2)
        self.assertEqual(plan["retire"], [])
        response = recall_module.Retriever(restored, self.ident, self.repo, now=BASE + 2).retrieve("zzforgotten", limit=10)
        refs = [item["ref"] for item in response["results"]]
        self.assertFalse(any(session_id in ref for ref in refs), refs)


if __name__ == "__main__":
    unittest.main()
