"""The alpha corpus preserves v1 questions and checks tied-minimum semantics."""

import unittest

from benchmarks.v2 import run_benchmark as v2


class BenchmarkV2Tests(unittest.TestCase):
    def test_preflight_preserves_the_historical_questions(self):
        corpus = v2.validate_corpus(v2.CORPUS, verbose=False)
        self.assertEqual(len(corpus["cases"]), 80)
        case = next(item for item in corpus["cases"] if item["id"] == "REJ-07")
        self.assertEqual((case["suite"], case["expect"]), ("adversarial", "execute"))

    def test_tie_preserving_plan_requires_typed_global_extremum(self):
        expected = {
            "any_of": [
                {"limit": 1},
                {"limit": None, "global_extremum": {
                    "table": "expenditures", "column": "per_pupil_expenditure",
                    "function": "MIN", "ties": "preserve",
                }},
            ],
        }
        program = {"limit": None, "global_extremum": {
            "table": "expenditures", "column": "per_pupil_expenditure",
            "function": "MIN", "ties": "preserve",
        }}
        self.assertTrue(v2.semantic_check(expected, program)[0])
        self.assertFalse(v2.semantic_check(expected, {"limit": None})[0])


if __name__ == "__main__":
    unittest.main()
