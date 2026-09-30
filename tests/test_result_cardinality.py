"""Global extremum cardinality stays distinct from ordinary ordering."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.result_cardinality import resolve_result_cardinality


class ResultCardinalityTests(unittest.TestCase):
    def test_singular_extremum_can_resolve_to_top_one(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"cardinality": {
            "choice": "top_one", "confidence": .96,
            "probabilities": {"top_one": .97, "all": .03},
        }}}, 90, 12, 60)
        result = resolve_result_cardinality(
            "What's the most common kind of school?",
            {"kind": "grouped", "target": "measure", "direction": "DESC"}, client)
        self.assertEqual(result.mode, "top_one")

    def test_singular_extremum_can_be_detected_before_ordering_is_resolved(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"cardinality": {
            "choice": "top_one", "confidence": .95,
            "probabilities": {"top_one": .96, "all": .04},
        }}}, 90, 12, 60)
        result = resolve_result_cardinality(
            "Which district has the lowest per-pupil expenditure?", None, client)
        self.assertEqual(result.mode, "top_one")
        state, _ = client.call.call_args.args
        self.assertIsNone(state["resolved_ordering"])

    def test_low_confidence_top_one_fails_open_to_all_rows(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"cardinality": {
            "choice": "top_one", "confidence": .55,
            "probabilities": {"top_one": .55, "all": .45},
        }}}, 90, 12, 60)
        result = resolve_result_cardinality(
            "show categories highest first",
            {"kind": "grouped", "target": "measure", "direction": "DESC"}, client)
        self.assertEqual(result.mode, "all")

    def test_decisive_probability_can_accept_top_one(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"cardinality": {
            "choice": "top_one", "confidence": .69,
            "probabilities": {"top_one": .85, "all": .15},
        }}}, 90, 12, 60)
        result = resolve_result_cardinality(
            "which category has the greatest count",
            {"kind": "grouped", "target": "measure", "direction": "DESC"}, client)
        self.assertEqual(result.mode, "top_one")


if __name__ == "__main__":
    unittest.main()
