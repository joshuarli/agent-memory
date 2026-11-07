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

    def test_dropped_span_hits_is_reported(self):
        baseline = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0, "span_hits": 3, "span_labeled": 4},
        }))
        current = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0, "span_hits": 2, "span_labeled": 4},
        }))
        lines = EVAL.compare(baseline, current)
        self.assertTrue(any("span_hits dropped" in line for line in lines), lines)

    def test_baseline_without_span_keys_is_not_gated(self):
        baseline = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0},
        }))
        current = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0, "span_hits": 0, "span_labeled": 3},
        }))
        self.assertEqual(EVAL.compare(baseline, current), [])

    def test_disappearing_span_keys_are_reported(self):
        baseline = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0, "span_hits": 3, "span_labeled": 4},
        }))
        current = dataset(default=summary(categories={
            "exact_identifier": {"hits": 5, "abstentions": 0},
        }))
        lines = EVAL.compare(baseline, current)
        self.assertTrue(any("span_hits disappeared" in line for line in lines), lines)


class SpanTests(unittest.TestCase):
    def test_span_matching_is_case_insensitive(self):
        case = {"expect": "match", "relevant": [], "answer": ["Xmx3g"]}
        results = [{"sessions": [], "tier": "lead", "text": "heap set to xmx3G"}]
        metrics = EVAL.case_metrics(case, results, {}, {})
        self.assertEqual(metrics["span5"], 1)

    def test_span_matching_normalizes_whitespace(self):
        case = {"expect": "match", "relevant": [], "answer": ["unique: [period: 3600, keys: [:user_id]]"]}
        results = [{"sessions": [], "tier": "lead", "text": "kept\n  unique:   [period: 3600,\tkeys: [:user_id]]  here"}]
        metrics = EVAL.case_metrics(case, results, {}, {})
        self.assertEqual(metrics["span5"], 1)

    def test_unlabeled_case_has_no_span5(self):
        results = [{"sessions": [], "tier": "lead", "text": "anything"}]
        metrics = EVAL.case_metrics({"expect": "match", "relevant": []}, results, {}, {})
        self.assertNotIn("span5", metrics)

    def test_unlabeled_case_excluded_from_category_span_counts(self):
        per_case = [
            ({"expect": "match", "category": "c", "answer": ["x"]}, {"recall5": 1, "span5": 1}),
            ({"expect": "match", "category": "c"}, {"recall5": 1}),
        ]
        counts = EVAL.category_counts(per_case)
        self.assertEqual(counts["c"]["span_labeled"], 1)
        self.assertEqual(counts["c"]["span_hits"], 1)


if __name__ == "__main__":
    unittest.main()
