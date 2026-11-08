#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK_TMP = ROOT / ".work" / "tmp"
WORK_TMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location("real_recall", ROOT / "eval/real_recall.py")
assert SPEC is not None
EVAL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = EVAL
SPEC.loader.exec_module(EVAL)

from agent_memory import learning  # noqa: E402
from agent_memory.store import Store, project_identity  # noqa: E402

BENCH = EVAL._bench()

QUESTION = "the ingest tests fail with ModuleNotFoundError orbit_core on a clean checkout"
ANSWER = "install the package editable into the runner environment"
BACK_REFERENCE = "we tried the ingest tests before with ModuleNotFoundError orbit_core, remember?"
LATER_TURN = "now rename the parser helper function so the name says what it returns"
MID_SESSION_TURN = "the parser helper now sits beside the schema loader"
UNRELATED = "zitrone quibble TIDEPOOLQUERYTOKEN"
ANSWER_AGE_DAYS = 20
FILLER_AGE_DAYS = 30
OPENING_AGE_DAYS = 10

WORLD = {}


def episode(key, turns, age):
    return {"id": key, "project": "alpha", "harness": "codex", "age_days": age, "turns": turns}


def fixture_spec():
    return {
        "now": "2026-10-20T12:00:00Z",
        "projects": {"alpha": {"files": {"src/app.py": "def main():\n    return 1\n"}}},
        "episodes": [
            episode("a-1", [
                {"user": QUESTION},
                {"assistant": "the editable install was missing again"},
            ], FILLER_AGE_DAYS),
            episode("a-2", [
                {"user": BACK_REFERENCE},
                {"assistant": "for the ingest tests and ModuleNotFoundError orbit_core on a clean checkout, "
                              + ANSWER + " so the import works"},
                {"user": MID_SESSION_TURN},
            ], ANSWER_AGE_DAYS),
            episode("a-3", [
                {"user": LATER_TURN},
                {"assistant": "Renamed it."},
            ], OPENING_AGE_DAYS),
        ],
        "memories": [],
        "mutations": [],
        "cases": [],
    }


def setUpModule():
    temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP), prefix="real-recall-")
    unittest.addModuleCleanup(temp.cleanup)
    world = BENCH.World(fixture_spec(), temp.name)
    world.build()
    store = Store(world.state_dir)
    store.refresh(roots=world.root_args())
    learning.initialize(store.db)
    store.close()
    WORLD.update(
        state_dir=world.state_dir,
        project=project_identity(world.projects["alpha"]["path"]),
        now=world.now,
        before_answer=world.now - 86400 * ANSWER_AGE_DAYS - 60,
        after_answer=world.now - 86400 * ANSWER_AGE_DAYS + 3600,
    )


class Sandbox(unittest.TestCase):
    def scratch(self):
        temp = tempfile.TemporaryDirectory(dir=str(WORK_TMP))
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = EVAL.main([str(value) for value in argv])
        return code, out.getvalue()

    def corpus(self, directory, records, name="corpus.jsonl"):
        path = Path(directory) / name
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path

    def record(self, **overrides):
        base = {
            "id": "case", "project": WORLD["project"], "session": "a-2", "position": 0,
            "cutoff": WORLD["now"], "query": QUESTION, "category": "as-of", "expect": "match",
            "answer": [ANSWER], "relevant_sessions": ["a-2"],
        }
        base.update(overrides)
        return base

    def scored(self, records):
        directory = self.scratch()
        corpus = self.corpus(directory, records)
        code, output = self.run_cli("score", "--state-dir", WORLD["state_dir"], "--corpus", corpus, "--json")
        self.assertEqual(code, 0)
        return json.loads(output), output


class CandidateTests(Sandbox):
    def proposals(self):
        directory = self.scratch()
        out = directory / "private" / "proposed.jsonl"
        code, output = self.run_cli("candidates", "--state-dir", WORLD["state_dir"], "--out", out)
        self.assertEqual(code, 0)
        return out, output

    def test_back_reference_is_proposed_and_non_matching_prompts_are_skipped(self):
        out, _output = self.proposals()
        proposals = [json.loads(line) for line in out.read_text().splitlines()]
        queries = [proposal["query"] for proposal in proposals]
        self.assertTrue(any(query.startswith("we tried the ingest tests before") for query in queries), queries)
        self.assertNotIn(MID_SESSION_TURN, queries)
        for proposal in proposals:
            self.assertEqual(set(proposal), {
                "id", "project", "session", "position", "cutoff", "query", "category", "expect", "answer", "relevant_sessions"})
            self.assertEqual((proposal["category"], proposal["expect"], proposal["answer"], proposal["relevant_sessions"]), ("", "", [], []))
            self.assertIsInstance(proposal["cutoff"], float)
            self.assertLessEqual(len(proposal["query"]), 400)
            self.assertEqual(proposal["project"], WORLD["project"])

    def test_opening_prompt_is_sampled_as_an_abstention_candidate(self):
        out, _output = self.proposals()
        proposals = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertIn(LATER_TURN, [proposal["query"] for proposal in proposals])

    def test_output_is_private_and_prints_no_prompt_text(self):
        out, output = self.proposals()
        self.assertEqual(stat.S_IMODE(out.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertNotIn("ingest", output)
        self.assertNotIn("orbit_core", output)


class ValidateTests(Sandbox):
    def test_match_without_an_answer_is_rejected_by_id(self):
        directory = self.scratch()
        corpus = self.corpus(directory, [self.record(id="bad-1", answer=[], query=UNRELATED)])
        code, output = self.run_cli("validate", "--corpus", corpus)
        self.assertEqual(code, 1)
        self.assertIn("bad-1", output)
        self.assertNotIn(UNRELATED, output)

    def test_unlabeled_line_is_accepted_and_counted(self):
        directory = self.scratch()
        corpus = self.corpus(directory, [self.record(id="open-1", expect="", category="", answer=[], relevant_sessions=[])])
        code, output = self.run_cli("validate", "--corpus", corpus)
        self.assertEqual(code, 0)
        self.assertIn("labeled=0 unlabeled=1", output)

    def test_abstain_with_an_answer_is_rejected(self):
        directory = self.scratch()
        corpus = self.corpus(directory, [self.record(id="bad-2", expect="abstain")])
        code, output = self.run_cli("validate", "--corpus", corpus)
        self.assertEqual(code, 1)
        self.assertIn("bad-2", output)


class ScoreTests(Sandbox):
    def test_cutoff_before_the_session_that_holds_the_answer_returns_no_hit(self):
        counts, _output = self.scored([self.record(id="early", cutoff=WORLD["before_answer"])])
        self.assertEqual(counts["as-of"]["hits"], 0)
        self.assertEqual(counts["as-of"]["span_hits"], 0)

    def test_cutoff_after_the_session_scores_a_span_hit(self):
        counts, _output = self.scored([self.record(id="late", cutoff=WORLD["after_answer"])])
        self.assertEqual(counts["as-of"]["hits"], 1)
        self.assertEqual((counts["as-of"]["span_hits"], counts["as-of"]["span_labeled"]), (1, 1))

    def test_abstain_line_with_no_matching_memory_scores_an_abstention(self):
        counts, _output = self.scored([self.record(
            id="quiet", category="abstain", expect="abstain", query=UNRELATED, answer=[], relevant_sessions=[])])
        self.assertEqual(counts["abstain"]["abstentions"], 1)
        self.assertEqual(counts["abstain"]["hits"], 0)

    def test_output_contains_no_query_text(self):
        _counts, output = self.scored([self.record(
            id="late", cutoff=WORLD["after_answer"], query=UNRELATED + " " + QUESTION)])
        self.assertNotIn("TIDEPOOLQUERYTOKEN", output)
        self.assertNotIn("ingest", output)
        self.assertNotIn("orbit_core", output)


if __name__ == "__main__":
    unittest.main()
