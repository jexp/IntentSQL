"""Aggregate resolution distinguishes the group measure from group ranking."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.aggregate import resolve_aggregate
from intentsql.skills.schema import Column


class AggregateTests(unittest.TestCase):
    def test_grouped_extremum_resolves_inner_measure(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "function": {"choice": "MAX", "confidence": .96},
            "target": {"choice": "c1", "confidence": .97},
            "extremum": {"choice": "MAX"},
        }}, 100, 12, 50)
        result = resolve_aggregate(
            "What year produced the smallest yearly maximum home-run total?",
            "performances",
            (Column("year", "INT", True, False), Column("HR", "INT", True, False)),
            client, group_key="year")
        self.assertEqual((result.function, result.column), ("MAX", "HR"))
        state, questions = client.call.call_args.args
        self.assertEqual(state["group_key"], "year")
        self.assertIn("per-group measure", questions["function"]["instructions"])


if __name__ == "__main__":
    unittest.main()
