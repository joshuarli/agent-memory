import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[2]
SRC = REPOSITORY / "agent-memory" / "src"
sys.path.insert(0, str(SRC))
from agent_memory import learning_ops as OPS  # noqa: E402
from agent_memory import store as STORE  # noqa: E402

SCRATCH_ROOT = REPOSITORY / ".work/tmp"
SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)


class AgentLearningOpsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def session_store(self):
        return patch.dict(sys.modules, {"agent_session_store": STORE})

    def create_database(self, state_dir, projects):
        state_dir.mkdir(parents=True, exist_ok=True)
        database_path = state_dir / "sessions.sqlite3"
        with sqlite3.connect(database_path) as db:
            db.execute(
                "CREATE TABLE learning_projects ("
                "project TEXT PRIMARY KEY, cwd TEXT NOT NULL, enabled INTEGER NOT NULL, "
                "enabled_at REAL NOT NULL, repository TEXT NOT NULL"
                ")"
            )
            db.executemany(
                "INSERT INTO learning_projects VALUES (?, ?, ?, ?, ?)", projects
            )
        return database_path

    def launchctl_runner(self, print_codes=None):
        calls = []
        codes = iter(print_codes or [])

        def run(command, **kwargs):
            calls.append(command)
            if command[1] == "print":
                returncode = next(codes, 0)
            else:
                returncode = 0
            return subprocess.CompletedProcess(command, returncode, "", "")

        return calls, run

    def test_hook_updates_one_hint_for_the_most_specific_enabled_project(self):
        state_dir = self.root / "state"
        repo = self.root / "repo"
        nested = repo / "nested"
        nested.mkdir(parents=True)
        database_path = self.create_database(
            state_dir,
            [
                ("root", str(repo), 1, 10.0, str(repo)),
                ("nested", str(nested), 1, 10.0, str(repo)),
            ],
        )
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(nested / "src"),
            "transcript_path": str(self.root / "not-read.jsonl"),
        }

        with self.session_store(), patch.object(OPS.time, "time", return_value=20.0):
            self.assertTrue(OPS.hook(state_dir, payload))

        with sqlite3.connect(database_path) as db:
            self.assertEqual(
                db.execute("SELECT project, observed_at FROM learning_hints").fetchall(),
                [("nested", 20.0)],
            )

    def test_hook_refreshes_the_existing_hint(self):
        state_dir = self.root / "state"
        repo = self.root / "repo"
        repo.mkdir()
        database_path = self.create_database(
            state_dir, [("repo", str(repo), 1, 10.0, str(repo))]
        )
        with sqlite3.connect(database_path) as db:
            db.execute(
                "CREATE TABLE learning_hints ("
                "project TEXT PRIMARY KEY, observed_at REAL NOT NULL)"
            )
            db.execute("INSERT INTO learning_hints VALUES ('repo', 15.0)")

        with self.session_store(), patch.object(OPS.time, "time", return_value=25.0):
            self.assertTrue(
                OPS.hook(state_dir, {"hook_event_name": "Stop", "cwd": str(repo)})
            )

        with sqlite3.connect(database_path) as db:
            self.assertEqual(
                db.execute("SELECT observed_at FROM learning_hints").fetchone(),
                (25.0,),
            )

    def test_hook_does_not_create_state_for_a_disabled_or_missing_project(self):
        state_dir = self.root / "state"
        repo = self.root / "repo"
        repo.mkdir()
        database_path = self.create_database(
            state_dir, [("repo", str(repo), 0, 10.0, str(repo))]
        )
        payload = {"hook_event_name": "Stop", "cwd": str(repo)}

        with self.session_store():
            self.assertFalse(OPS.hook(state_dir, payload))
            self.assertFalse(OPS.hook(self.root / "absent", payload))

        with sqlite3.connect(database_path) as db:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        self.assertEqual([name for (name,) in tables], ["learning_projects"])
        self.assertFalse((self.root / "absent" / "sessions.sqlite3").exists())

    def test_hook_ignores_workers_and_non_stop_events(self):
        state_dir = self.root / "state"
        repo = self.root / "repo"
        repo.mkdir()
        database_path = self.create_database(
            state_dir, [("repo", str(repo), 1, 10.0, str(repo))]
        )
        payload = {"hook_event_name": "Stop", "cwd": str(repo)}

        with self.session_store(), patch.dict(os.environ, {"AGENT_LEARN_WORKER": "1"}):
            self.assertFalse(OPS.hook(state_dir, payload))
        with self.session_store():
            self.assertFalse(
                OPS.hook(
                    state_dir,
                    {"hook_event_name": "SubagentStop", "cwd": str(repo)},
                )
            )
            self.assertFalse(OPS.hook(state_dir, {"hook_event_name": "Stop"}))

        with sqlite3.connect(database_path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'learning_hints'"
                ).fetchone(),
                None,
            )

    def test_scheduler_status_does_not_create_launch_agent_directories(self):
        home = self.root / "home"
        home.mkdir()
        calls, runner = self.launchctl_runner([1])

        with patch.object(OPS.sys, "platform", "darwin"):
            status = OPS.scheduler_status(home, runner)

        self.assertEqual(status, OPS.SchedulerStatus(True, False, False))
        self.assertEqual(calls[0][1], "print")
        self.assertFalse((home / "Library").exists())

    def test_scheduler_status_separates_configured_from_loaded(self):
        home = self.root / "home"
        directory = home / "Library/LaunchAgents"
        directory.mkdir(parents=True)
        (directory / OPS.LAUNCHD_FILENAME).write_bytes(
            plistlib.dumps({"Label": OPS.LAUNCHD_LABEL})
        )
        _, not_loaded = self.launchctl_runner([1])
        _, loaded = self.launchctl_runner([0])

        with patch.object(OPS.sys, "platform", "darwin"):
            self.assertEqual(
                OPS.scheduler_status(home, not_loaded),
                OPS.SchedulerStatus(True, True, False),
            )
            self.assertEqual(
                OPS.scheduler_status(home, loaded),
                OPS.SchedulerStatus(True, True, True),
            )

    def test_scheduler_install_writes_template_and_bootstraps_only_through_runner(self):
        home = self.root / "home"
        home.mkdir()
        state_dir = self.root / "state"
        state_dir.mkdir()
        calls, runner = self.launchctl_runner([1, 0])

        with patch.object(OPS.sys, "platform", "darwin"):
            plist_path = OPS.install_scheduler(REPOSITORY, state_dir, home, runner)

        with plist_path.open("rb") as stream:
            plist = plistlib.load(stream)
        self.assertEqual(plist["Label"], OPS.LAUNCHD_LABEL)
        self.assertEqual(
            plist["ProgramArguments"],
            [
                str(home / ".local/bin/agent-learn"),
                "--state-dir",
                str(state_dir),
                "run",
                "--scheduled",
            ],
        )
        self.assertEqual(plist["StartInterval"], 900)
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual([call[1] for call in calls], ["print", "bootstrap", "print"])

    def test_scheduler_install_refuses_a_symlinked_launch_agents_directory(self):
        home = self.root / "home"
        external = self.root / "external"
        home.mkdir()
        external.mkdir()
        (home / "Library").symlink_to(external, target_is_directory=True)
        state_dir = self.root / "state"
        state_dir.mkdir()
        calls, runner = self.launchctl_runner([1])

        with patch.object(OPS.sys, "platform", "darwin"):
            with self.assertRaises(OSError):
                OPS.install_scheduler(REPOSITORY, state_dir, home, runner)

        self.assertEqual(calls, [])
        self.assertEqual(list(external.iterdir()), [])

    def test_scheduler_install_preserves_a_conflicting_launch_agent(self):
        home = self.root / "home"
        directory = home / "Library/LaunchAgents"
        directory.mkdir(parents=True)
        plist_path = directory / OPS.LAUNCHD_FILENAME
        plist_path.write_bytes(plistlib.dumps({"Label": "other.agent"}))
        original = plist_path.read_bytes()
        state_dir = self.root / "state"
        state_dir.mkdir()
        calls, runner = self.launchctl_runner([1])

        with patch.object(OPS.sys, "platform", "darwin"):
            with self.assertRaises(FileExistsError):
                OPS.install_scheduler(REPOSITORY, state_dir, home, runner)

        self.assertEqual(plist_path.read_bytes(), original)
        self.assertEqual(calls, [])

    def test_scheduler_remove_unloads_and_removes_its_owned_file(self):
        home = self.root / "home"
        directory = home / "Library/LaunchAgents"
        directory.mkdir(parents=True)
        plist_path = directory / OPS.LAUNCHD_FILENAME
        plist_path.write_bytes(plistlib.dumps({"Label": OPS.LAUNCHD_LABEL}))
        calls, runner = self.launchctl_runner([0])

        with patch.object(OPS.sys, "platform", "darwin"):
            removed = OPS.remove_scheduler(home, runner)

        self.assertTrue(removed)
        self.assertFalse(plist_path.exists())
        self.assertEqual([call[1] for call in calls], ["print", "bootout"])

    def test_linux_reports_manual_scheduler_and_rejects_launchd_install(self):
        home = self.root / "home"
        home.mkdir()
        state_dir = self.root / "state"
        state_dir.mkdir()
        calls, runner = self.launchctl_runner()

        with patch.object(OPS.sys, "platform", "linux"):
            status = OPS.scheduler_status(home, runner)
            with self.assertRaises(OPS.SchedulerError):
                OPS.install_scheduler(REPOSITORY, state_dir, home, runner)

        self.assertEqual(status, OPS.SchedulerStatus(False, False, None))
        self.assertEqual(calls, [])
        self.assertFalse((home / "Library").exists())


if __name__ == "__main__":
    unittest.main()
