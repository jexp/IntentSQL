"""Joined ordering is selected only from inspected columns."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.join_ordering import resolve_join_ordering
from intentsql.skills.schema import Column


class JoinOrderingTests(unittest.TestCase):
    def test_qualified_order_is_typed(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c1"}, "direction": {"choice": "DESC"}}}, 100, 30, 200)
        result = resolve_join_ordering("Sort by pupils descending", {
            "districts": (Column("name", "TEXT", True, False),),
            "expenditures": (Column("pupils", "INTEGER", True, False),)}, client)
        self.assertEqual((result.field.table, result.field.column, result.direction),
                         ("expenditures", "pupils", "DESC"))
        self.assertEqual(result.jev_calls, 1)

    def test_ungrounded_join_ranking_can_choose_none(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "none"}, "direction": {"choice": "DESC"}}},
            100, 30, 200)
        result = resolve_join_ordering("Show the happiest schools", {
            "schools": (Column("name", "TEXT", True, False),),
            "graduation_rates": (Column("graduated", "REAL", True, False),)}, client)
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.field)
        _, questions = client.call.call_args.args
        self.assertIn("none", questions["target"]["criteria"])
