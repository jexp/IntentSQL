"""A rounding skill may select only the supplied bounded transformations."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.rounding import resolve_rounding


class RoundingTests(unittest.TestCase):
    def test_rounding_is_typed_and_bounded(self):
        client = Mock()
        client.call.return_value = CallResult(
            {"answers": {"rounding": {"choice": "2", "confidence": .9,
                                      "probabilities": {"2": .9}}}}, 100, 20, 300)
        result = resolve_rounding("Average salary rounded to two decimals", "AVG(salary)", client)
        self.assertEqual((result.status, result.places, result.jev_calls), ("resolved", 2, 1))
        self.assertIsNotNone(result.fact_id)
        state, questions = client.call.call_args.args
        self.assertEqual(state["measure"], "AVG(salary)")
        self.assertIn("unsupported", questions["rounding"]["criteria"])

    def test_non_rounding_transformation_stays_unsupported(self):
        client = Mock()
        client.call.return_value = CallResult(
            {"answers": {"rounding": {"choice": "unsupported"}}}, 100, 20, 300)
        result = resolve_rounding("salary divided by hits", "AVG(salary)", client)
        self.assertEqual(result.status, "unsupported")
        self.assertIsNone(result.places)

    def test_rounding_fact_provenance_does_not_claim_unrelated_equal_literals(self):
        client = Mock()
        client.call.return_value = CallResult(
            {"answers": {"rounding": {"choice": "2", "confidence": .9,
                                      "probabilities": {"2": .9}}}}, 100, 20, 300)
        result = resolve_rounding(
            "Compare year 2 with year 3 and round the average to 2 decimals",
            "AVG(value)", client)
        self.assertIsNone(result.fact_id)
