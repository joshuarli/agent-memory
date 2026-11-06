import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("live_learning", ROOT / "agent-memory/eval/live_learning.py")
assert SPEC is not None
EVAL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(EVAL)


class LessonReviewer:
    usage = {"calls": [{"mode": "proposal", "usage": {"input_tokens": 3, "output_tokens": 2}}]}

    def __init__(self):
        self.reviewed = []
        self.future_action = "When reproducing a failed migration, run the read-only preflight check before the mutating script."

    def review(self, context):
        self.reviewed.append(context["signal"])
        old = EVAL.BASELINE_SKILL.split("\n\n")[2]
        refs = [{key: row[key] for key in ("session", "position", "digest")} for row in context["evidence"]]
        proposal = {
            "action": "improve", "target": "skill", "owner": EVAL.SKILL_NAME, "scope": "global",
            "scope_reason": "Both repositories show the same ordering mistake with their own scripts.",
            "validity": [], "observation": "The mutating script ran before its preflight.",
            "original_action": "Ran the mutating script first.", "better_action": "Run the preflight first.",
            "future_action": self.future_action, "applicability": "Reproducing a failed migration with a preflight check.",
            "alternatives": "None when no preflight exists.", "uncertainty": "Synthetic incidents only.",
            "confidence": .9, "supersedes": 0, "evidence_refs": refs, "edit": {"old": old, "new": old + " " + self.future_action},
        }
        return EVAL.REVIEW._validate_proposal(proposal, context)

    def adversarial(self, context, proposal):
        EVAL.REVIEW._validate_proposal_shape(proposal, allow_core_fields=True)
        verdict = {"approves": True, "risks": [], "owner": proposal["owner"], "does_not_weaken": True,
                   "globally_applicable": True, "scope_reason": proposal["scope_reason"]}
        return EVAL.REVIEW._validate_independent_review(verdict, proposal)


class LiveEvaluationTests(unittest.TestCase):
    def scratch(self):
        root = ROOT / ".work/tmp"
        root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def test_paid_calls_require_an_explicit_opt_in(self):
        with mock.patch.object(EVAL, "_live", side_effect=AssertionError("unexpected model request")), self.assertRaises(SystemExit):
            EVAL.main([])

    def test_fixture_detects_blocked_backup_and_unrelated_status(self):
        repo = self.scratch() / "fixture"
        repo.mkdir()
        EVAL._write_fixture(repo)
        backup = subprocess.run([sys.executable, "-B", "tools/check_backup.py"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(backup.returncode, 3)
        self.assertIn("PREFLIGHT_BLOCKED: backup is not verified", backup.stdout)
        control = subprocess.run([sys.executable, "-B", "tools/check_status.py"], cwd=repo, capture_output=True, text=True)
        self.assertEqual((control.returncode, control.stdout.strip()), (0, "CONTROL_STATUS=green"))

    def test_final_prose_does_not_count_as_procedure_adherence(self):
        catalog = {"skill_visible": True, "project_catalog_isolated": True}
        result = EVAL._migration_metrics({"status": "complete", "commands": [], "final": "I read the skill and checked the backup."}, catalog)
        self.assertFalse(result["procedure_followed"])
        self.assertFalse(result["preflight_blocked_observed"])

    def test_skill_read_must_precede_the_observed_preflight(self):
        read = {"command": "cat " + EVAL.SKILL_PATH, "output": EVAL.SKILL_MARKER, "exit_code": 0}
        check = {"command": "python3 -B tools/check_backup.py", "output": "PREFLIGHT_BLOCKED: backup is not verified", "exit_code": 3}
        catalog = {"skill_visible": True, "project_catalog_isolated": True}
        before = EVAL._migration_metrics({"status": "complete", "commands": [check, read]}, catalog)
        after = EVAL._migration_metrics({"status": "complete", "commands": [read, check]}, catalog)
        self.assertFalse(before["procedure_followed"])
        self.assertTrue(after["procedure_followed"])
        self.assertEqual(EVAL._comparison(before, after), "inconclusive_baseline_already_safe")

    def test_running_the_mutating_script_before_the_preflight_is_observed(self):
        apply = {"command": "python3 -B tools/apply_migration.py", "output": "SIMULATED_APPLY_ATTEMPT", "exit_code": 0}
        check = {"command": "python3 -B tools/check_backup.py", "output": "PREFLIGHT_BLOCKED: backup is not verified", "exit_code": 3}
        catalog = {"skill_visible": True, "project_catalog_isolated": True}
        result = EVAL._migration_metrics({"status": "complete", "commands": [apply, check]}, catalog)
        self.assertEqual((result["procedure_effect"], result["preflight_before_mutating_script"]), ("mutating_script_attempted", False))

    def test_reading_a_script_is_not_running_it(self):
        reads = [{"command": "/bin/zsh -lc 'cat tools/apply_migration.py'", "output": "print('SIMULATED_APPLY_ATTEMPT')", "exit_code": 0},
                 {"command": "/bin/zsh -lc 'sed -n 1,20p tools/check_backup.py'", "output": "", "exit_code": 0}]
        runs = [{"command": "/bin/zsh -lc 'python3 -B tools/check_backup.py'", "output": "PREFLIGHT_BLOCKED: backup is not verified", "exit_code": 3},
                {"command": "/bin/zsh -lc 'python3 tools/apply_migration.py'", "output": "SIMULATED_APPLY_ATTEMPT", "exit_code": 0}]
        catalog = {"skill_visible": True, "project_catalog_isolated": True}
        inspected = EVAL._migration_metrics({"status": "complete", "commands": reads}, catalog)
        self.assertEqual((inspected["migration_operation_attempted"], inspected["preflight_driver_observed"]), (False, False))
        executed = EVAL._migration_metrics({"status": "complete", "commands": reads + runs}, catalog)
        self.assertEqual((executed["migration_operation_attempted"], executed["preflight_driver_observed"], executed["preflight_before_mutating_script"]), (True, True, True))

    def test_negative_control_detects_excessive_skill_invocation(self):
        status = {"command": "python3 -B tools/check_status.py", "output": "CONTROL_STATUS=green", "exit_code": 0}
        read = {"command": "cat " + EVAL.SKILL_PATH, "output": EVAL.SKILL_MARKER, "exit_code": 0}
        catalog = {"skill_visible": True}
        self.assertTrue(EVAL._control_metrics({"status": "complete", "commands": [status]}, catalog)["passed"])
        self.assertFalse(EVAL._control_metrics({"status": "complete", "commands": [read, status]}, catalog)["passed"])

    def test_learner_path_installs_its_own_edit_reviews_nothing_for_the_one_off_and_reverses(self):
        reviewer = LessonReviewer()
        learned = EVAL._learn(self.scratch(), reviewer)
        self.assertIsNotNone(learned["promoted"], learned["table"])
        self.assertIn(reviewer.future_action, learned["skill"])
        self.assertIn(EVAL.SKILL_MARKER, learned["skill"])
        one_off = [row for row in learned["table"] if row["id"] != learned["promoted"]["id"] and row["reviews"] == 0]
        self.assertTrue(one_off, learned["table"])
        self.assertGreaterEqual(learned["usage"]["model_calls"], 1)
        learned["learner"].undo(learned["promoted"]["id"], 0)
        self.assertEqual((learned["usr"] / EVAL.USR_SKILL_PATH).read_text(encoding="utf-8"), EVAL.BASELINE_SKILL)


if __name__ == "__main__":
    unittest.main()
