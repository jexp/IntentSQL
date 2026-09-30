"""Aggregate shape refinement keeps grouped extrema out of nested aggregates."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.aggregate_shape import resolve_aggregate_shape
from intentsql.skills.schema import Column


class AggregateShapeTests(unittest.TestCase):
    def test_row_extremum_is_a_supported_shape(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"shape": {
            "choice": "row_extremum", "confidence": .9},
            "stored_row_extremum": {"noul": .95}}}, 100, 10, 1)
        result = resolve_aggregate_shape(
            "which district has the smallest pupil count", "expenditures",
            (Column("pupils", "INTEGER", True, False),), client)
        self.assertEqual((result.shape, result.status), ("row_extremum", "resolved"))
    def test_smallest_groupwise_maximum_is_grouped(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"shape": {
            "choice": "grouped", "confidence": .97,
            "probabilities": {"grouped": .97, "scalar": .02, "unsupported_nested": .01},
        }, "stored_row_extremum": {"noul": .08}}}, 100, 12, 50)
        result = resolve_aggregate_shape(
            "What year produced the smallest yearly maximum home-run total?",
            "performances",
            (Column("year", "INT", True, False), Column("HR", "INT", True, False)),
            client)
        self.assertEqual((result.shape, result.status), ("grouped", "resolved"))
        state, _ = client.call.call_args.args
        self.assertIn("highest/lowest", state["task"])

    def test_genuine_nested_aggregate_can_fail_closed(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"shape": {
            "choice": "unsupported_nested", "confidence": .95,
        }, "stored_row_extremum": {"noul": .05}}}, 80, 10, 40)
        result = resolve_aggregate_shape(
            "What is the average of the department averages?", "employees",
            (Column("department", "TEXT", True, False), Column("salary", "INT", True, False)),
            client)
        self.assertEqual(result.status, "unsupported")


if __name__ == "__main__":
    unittest.main()
