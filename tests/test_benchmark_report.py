"""Aggregate benchmark measurements must not imply stability after one run."""

import unittest

from benchmarks.v1.run_benchmark import subset_dict, summarize


class BenchmarkReportTests(unittest.TestCase):
    def test_in_alternative_order_is_not_a_semantic_failure(self):
        expected = {"column": "type", "operator": "IN", "value": ["Public", "Charter"]}
        actual = {"column": "type", "operator": "IN", "value": ["Charter", "Public"]}
        self.assertTrue(subset_dict(expected, actual))
        self.assertFalse(subset_dict(expected, {**actual, "value": ["Charter", "Private"]}))

    def test_totals_and_single_run_stability(self):
        records = [
            {"id": "A", "suite": "core", "expect": "execute", "full_pass": True,
             "semantic_pass": True, "execution_pass": True, "elapsed_s": 2.0,
             "stats": {"jev_calls": 2, "input_tokens": 100, "output_tokens": 20,
                       "cost_usd": .001}},
            {"id": "B", "suite": "reject", "expect": "reject", "full_pass": True,
             "unsafe_execution": False, "elapsed_s": 4.0,
             "stats": {"jev_calls": 4, "input_tokens": 300, "output_tokens": 60,
                       "cost_usd": .003}},
        ]
        summary = summarize(records, 1, 7.5)
        self.assertIsNone(summary["stable_cases"])
        self.assertIsNone(summary["stable_case_rate"])
        self.assertEqual(summary["jev"]["total_calls"], 6)
        self.assertEqual(summary["jev"]["total_input_tokens"], 400)
        self.assertEqual(summary["jev"]["total_output_tokens"], 80)
        self.assertEqual(summary["jev"]["total_tokens"], 480)
        self.assertEqual(summary["jev"]["median_tokens"], 240)
        self.assertAlmostEqual(summary["jev"]["total_cost_usd"], .004)
        self.assertEqual(summary["wall_clock_s"], 7.5)


if __name__ == "__main__":
    unittest.main()
