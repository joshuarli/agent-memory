#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bench_recall", ROOT / "eval/bench_recall.py")
assert SPEC is not None
EVAL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(EVAL)


def summary(hits=5, abstentions=2, leakage=0, stale=0, size=100.0, categories=None):
    return {
        "categories": categories if categories is not None else {
            "exact_identifier": {"hits": hits, "abstentions": 0},
            "negative": {"hits": 0, "abstentions": abstentions},
        },
        "leakage_violations": leakage,
        "stale_instruction_violations": stale,
        "mean_result_bytes": size,
    }


def dataset(default=None, retries=None):
    return {"dev": {"default": default or summary(), "retries": retries or summary()}}


class CompareTests(unittest.TestCase):
    def test_no_regression_returns_empty(self):
        self.assertEqual(EVAL.compare(dataset(), dataset()), [])

    def test_dropped_hit_count_is_reported(self):
        lines = EVAL.compare(dataset(), dataset(default=summary(hits=4)))
        self.assertTrue(any("hits dropped" in line for line in lines), lines)

    def test_dropped_abstention_count_is_reported(self):
        lines = EVAL.compare(dataset(), dataset(default=summary(abstentions=1)))
        self.assertTrue(any("abstentions dropped" in line for line in lines), lines)

    def test_new_leakage_violation_is_reported(self):
        lines = EVAL.compare(dataset(), dataset(default=summary(leakage=1)))
        self.assertTrue(any("leakage violations rose" in line for line in lines), lines)

    def test_new_stale_instruction_violation_is_reported(self):
        lines = EVAL.compare(dataset(), dataset(default=summary(stale=1)))
        self.assertTrue(any("stale instruction violations rose" in line for line in lines), lines)

    def test_more_than_ten_percent_byte_growth_is_reported(self):
        lines = EVAL.compare(dataset(), dataset(default=summary(size=111.0)))
        self.assertTrue(any("mean result bytes grew" in line for line in lines), lines)

    def test_ten_percent_byte_growth_is_not_reported(self):
        self.assertEqual(EVAL.compare(dataset(), dataset(default=summary(size=110.0))), [])

    def test_missing_category_is_reported(self):
        baseline = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0},
            "temporal": {"hits": 1, "abstentions": 0},
        }))
        current = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0},
        }))
        lines = EVAL.compare(baseline, current)
        self.assertTrue(any("temporal" in line and "missing" in line for line in lines), lines)

    def test_gain_is_not_reported_as_failure(self):
        current = dataset(default=summary(hits=6, abstentions=3, size=90.0))
        self.assertEqual(EVAL.compare(dataset(), current), [])


if __name__ == "__main__":
    unittest.main()
