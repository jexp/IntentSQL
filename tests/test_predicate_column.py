"""Predicate columns are selected from inspected columns only."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.predicate import resolve_predicate_column
from intentsql.skills.schema import Column


class PredicateColumnTests(unittest.TestCase):
    def test_only_column_is_automatic(self):
        client = Mock()
        result = resolve_predicate_column("only active", "flags",
                                          (Column("active", "INTEGER", True, False),), client)
        self.assertEqual(result.column, "active")
        client.call.assert_not_called()

    def test_choices_are_schema_generated(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"column": {
            "choice": "c1", "confidence": .94,
            "probabilities": {"c0": .04, "c1": .96}}}}, 350, 45, 650)
        result = resolve_predicate_column("widgets over 10", "widgets",
                    (Column("name", "TEXT", True, False),
                     Column("weight", "REAL", True, False)), client)
        self.assertEqual(result.column, "weight")
        state, questions = client.call.call_args.args
        self.assertEqual(state["source_table"], "widgets")
        self.assertEqual(questions["column"]["criteria"]["c1"]["column"], "weight")

    def test_group_key_competition_gets_targeted_refinement(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"column": {"choice": "c0",
                "probabilities": {"c0": .51, "c1": .46}}}}, 200, 30, 300),
            CallResult({"answers": {"column": {"choice": "c1",
                "probabilities": {"c1": .9, "c0": .1}}}}, 90, 15, 200),
        ]
        result = resolve_predicate_column("group counts for public items", "items", (
            Column("city", "TEXT", True, False),
            Column("type", "TEXT", True, False)), client, group_column="city")
        self.assertEqual((result.column, result.jev_calls), ("type", 2))


if __name__ == "__main__":
    unittest.main()
