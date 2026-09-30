"""Quantity uses literal facts and Jev roles, without phrase interpretation."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.facts import extract_facts
from intentsql.skills.quantity import resolve_quantity


class QuantitySkillTests(unittest.TestCase):
    def test_numeric_route_recovers_exact_worded_literal_without_inventing_digits(self):
        client = Mock()
        result = resolve_quantity("Give me the five largest records", "numeric", client)
        self.assertEqual(result.value, 5)
        self.assertEqual(result.status, "bounded")
        client.call.assert_not_called()
    def test_fact_extraction_does_not_assign_roles(self):
        facts = extract_facts('Show 5 rows from 2004-12-31 with id 17 and name "Row 2"')
        self.assertEqual([(fact.kind, fact.value) for fact in facts],
                         [("integer", 5), ("date", "2004-12-31"),
                          ("integer", 17), ("quoted_text", "Row 2")])

    def test_single_numeric_limit_needs_no_call_after_semantic_route(self):
        client = Mock()
        result = resolve_quantity("show 5 records", "numeric", client)
        self.assertEqual((result.value, result.jev_calls), (5, 0))
        client.call.assert_not_called()

    def test_multiple_numbers_require_role_choice(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"limit": {
            "choice": "n0", "confidence": .97,
            "probabilities": {"n0": .98, "n1": .02}}}}, 300, 40, 700)
        result = resolve_quantity("show 5 rows from season 2", "numeric", client)
        self.assertEqual(result.value, 5)
        self.assertEqual(result.jev_calls, 1)
        _, questions = client.call.call_args.args
        self.assertEqual(questions["limit"]["criteria"]["n1"]["value"], 2)

    def test_single_worded_limit_reuses_exact_fact_without_jev_redecoding(self):
        client = Mock()
        result = resolve_quantity("show five records", "worded", client)
        self.assertEqual((result.value, result.jev_calls, result.source),
                         (5, 0, "single_worded_literal"))
        client.call.assert_not_called()

    def test_worded_limit_with_other_numeric_role_uses_bounded_role_choice(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"limit": {
            "choice": "w0", "confidence": .97,
            "probabilities": {"w0": .98, "none": .02}}}}, 120, 20, 80)
        result = resolve_quantity("show five rows from season two", "worded", client,
                                  other_numeric_roles=True)
        self.assertEqual((result.value, result.jev_calls), (5, 1))
        _, questions = client.call.call_args.args
        self.assertIn("none", questions["limit"]["criteria"])


if __name__ == "__main__":
    unittest.main()
