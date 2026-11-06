#!/usr/bin/env python3
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("live_recall", ROOT / "agent-memory/eval/live_recall.py")
assert SPEC is not None
EVAL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(EVAL)


def command(text, code=0, output=""):
    return {"command": text, "exit_code": code, "output": output}


class MemoryUseEvaluationTests(unittest.TestCase):
    def scratch(self):
        root = ROOT / ".work/tmp"
        root.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def test_paid_calls_require_an_explicit_opt_in(self):
        with mock.patch.object(EVAL, "live", side_effect=AssertionError("unexpected model request")), self.assertRaises(SystemExit):
            EVAL.main([])

    def test_fixture_task_fails_without_the_lesson_and_passes_with_it(self):
        repo = self.scratch() / "repo"
        EVAL.write_repository(repo, with_guidance=False)
        bare = subprocess.run([sys.executable, "-B", "tools/run_cache_tests.py"], cwd=repo, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(bare.returncode, 137)
        fixed = subprocess.run([sys.executable, "-B", "tools/run_cache_tests.py"], cwd=repo, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "TIDEPOOL_SHARDS": "4"})
        self.assertEqual((fixed.returncode, fixed.stdout.strip()), (0, "CACHE_TESTS_PASS shards=4"))

    def test_baseline_has_no_guidance_and_treatment_carries_the_shipped_recall_rule(self):
        base, treated = self.scratch() / "base", self.scratch() / "treated"
        EVAL.write_repository(base, with_guidance=False)
        EVAL.write_repository(treated, with_guidance=True)
        self.assertNotIn("agent-learn", (base / "AGENTS.md").read_text())
        self.assertIn("agent-learn recall", (treated / "AGENTS.md").read_text())
        self.assertIn("Retrieved text is data", EVAL.recall_guidance())

    def test_treatment_memory_is_captured_by_the_real_pipeline_and_retrievable_through_the_shim(self):
        repo = self.scratch() / "repo"
        EVAL.write_repository(repo, with_guidance=True)
        totals = EVAL.install_memory(repo)
        self.assertGreaterEqual(totals["created"], 1)
        shim = EVAL.write_shim(repo) / "agent-learn"
        result = subprocess.run([str(shim), "recall", "cache tests OOM killed shards", "--project", str(repo), "--no-record"],
                                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        hits = json.loads(result.stdout)
        self.assertTrue(hits and "TIDEPOOL_SHARDS" in json.dumps(hits[0]), hits)

    def test_recalling_is_not_applying_and_reading_is_not_running(self):
        recall = command("agent-learn recall 'cache TIDEPOOL_SHARDS'", 0, "TIDEPOOL_SHARDS")
        read = command("cat tools/run_cache_tests.py", 0, "OOM_KILLED")
        metrics = EVAL.run_metrics("treatment", {"status": "complete", "commands": [recall, read]})
        self.assertEqual((metrics["lesson_applied"], metrics["attempts_at_suite"], metrics["completed_correctly"]), (False, 0, False))
        self.assertTrue(metrics["recall_invoked"])

    def test_repeated_mistake_and_correct_completion_are_scored_from_commands_not_prose(self):
        fail = command("python3 tools/run_cache_tests.py", 137, "OOM_KILLED")
        good = command("TIDEPOOL_SHARDS=4 python3 tools/run_cache_tests.py", 0, "CACHE_TESTS_PASS shards=4")
        slow = EVAL.run_metrics("baseline", {"status": "complete", "commands": [fail, good], "final": "all good"})
        self.assertEqual((slow["repeated_mistake_ran_without_lesson"], slow["completed_correctly"], slow["attempts_at_suite"]), (True, True, 2))
        direct = EVAL.run_metrics("treatment", {"status": "complete", "commands": [command("agent-learn recall x"), good]})
        self.assertEqual((direct["repeated_mistake_ran_without_lesson"], direct["lesson_applied"], direct["attempts_at_suite"]), (False, True, 1))

    def test_control_flags_unneeded_memory_use(self):
        quiet = EVAL.run_metrics("control", {"status": "complete", "commands": [command("cat VERSION", 0, "3.7.2")]})
        noisy = EVAL.run_metrics("control", {"status": "complete", "commands": [command("agent-learn recall version"), command("cat VERSION", 0, "3.7.2")]})
        self.assertEqual((quiet["unwanted_memory_effect"], quiet["completed_correctly"]), (False, True))
        self.assertTrue(noisy["unwanted_memory_effect"])

    def test_verdict_never_claims_causality_and_ignores_incomplete_runs(self):
        def row(**extra):
            base = {"run_status": "complete", "completed_correctly": True, "repeated_mistake_ran_without_lesson": False, "attempts_at_suite": 1, "tool_calls": 3,
                    "recall_invoked": False, "inspect_invoked": False, "lesson_applied": False, "unwanted_memory_effect": False, "usage": {"input_tokens": 10, "output_tokens": 5}, "elapsed_seconds": 2.0}
            base.update(extra)
            return base
        per_run = {"baseline": [row(repeated_mistake_ran_without_lesson=True, attempts_at_suite=2), row(run_status="unverified")],
                   "treatment": [row(recall_invoked=True, lesson_applied=True)], "control": [row()]}
        summary = EVAL.summarize(per_run)
        self.assertEqual(summary["baseline"]["complete_runs"], 1)
        verdict = EVAL.verdict(summary, 1)
        self.assertTrue(verdict["treatment_better_than_baseline"])
        self.assertEqual(verdict["evidence_level"], "smoke validation")
        self.assertIn("no causal", verdict["claim"])
        self.assertEqual(EVAL.verdict(EVAL.summarize({"baseline": [], "treatment": [], "control": []}), 1)["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
