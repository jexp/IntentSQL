"""Aggregate choices remain bounded to inspected columns."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.aggregate import resolve_aggregate
from intentsql.skills.schema import Column


class AggregateSkillTests(unittest.TestCase):
    def test_function_and_target_share_one_call(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "function": {"choice": "AVG"}, "target": {"choice": "c1"},
            "extremum": {"choice": "neither"}}}, 200, 30, 300)
        result = resolve_aggregate("average amount", "expenses", (
            Column("id", "INT", False, True), Column("amount", "REAL", True, False)), client)
        self.assertEqual((result.function, result.column, result.jev_calls), ("AVG", "amount", 1))


if __name__ == "__main__":
    unittest.main()
