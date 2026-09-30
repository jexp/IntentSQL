"""The frozen applicable CS50 spec subset has executable independent oracles."""

import unittest
from pathlib import Path

from benchmarks.cs50_cases import ALPHA_APPLICABLE_IDS, ALPHA_EXCLUSIONS, CASES
from intentsql.database import connect


class CS50SpecFixtureTests(unittest.TestCase):
    def test_every_case_is_classified_once(self):
        ids = {case["id"] for case in CASES}
        applicable = set(ALPHA_APPLICABLE_IDS)
        excluded = set(ALPHA_EXCLUSIONS)
        self.assertFalse(applicable & excluded)
        self.assertEqual(applicable | excluded, ids)
        self.assertEqual(len(ALPHA_APPLICABLE_IDS), len(applicable))

    def test_applicable_reference_queries_match_frozen_dimensions(self):
        root = Path(__file__).resolve().parents[1]
        selected = {case["id"]: case for case in CASES
                    if case["id"] in ALPHA_APPLICABLE_IDS}
        for case_id in ALPHA_APPLICABLE_IDS:
            case = selected[case_id]
            with self.subTest(case=case_id), connect(
                    root / "data" / case["database"]) as connection:
                rows = connection.execute(case["gold_sql"]).fetchall()
                self.assertEqual(len(rows), case["expected_rows"])

    def test_exclusions_name_relational_capability_not_language_wording(self):
        for case_id, reason in ALPHA_EXCLUSIONS.items():
            with self.subTest(case=case_id):
                self.assertTrue(reason)
                self.assertNotIn("prompt", reason.casefold())


if __name__ == "__main__":
    unittest.main()
