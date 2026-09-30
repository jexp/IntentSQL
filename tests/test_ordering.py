"""Ordering choices are generated from the current table only."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.ordering import (resolve_ordering, resolve_single_row_order_need,
                                       review_order_direction)
from intentsql.skills.schema import Column


class OrderingTests(unittest.TestCase):
    def test_single_row_extremum_need_is_a_separate_bounded_judgment(self):
        client = Mock()
        client.call.return_value = CallResult(
            {"answers": {"requires_order": {"noul": .92}}}, 40, 5, 10)
        result = resolve_single_row_order_need("Find the single minimum record", client)
        self.assertTrue(result.needed)
        _, questions = client.call.call_args.args
        self.assertEqual(questions["requires_order"]["type"], "noul")

    def test_target_and_direction_share_one_call(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c1"}, "direction": {"choice": "DESC"},
            "direction_explicit": {"noul": .95},
            "secondary_target": {"choice": "none"},
            "secondary_direction": {"choice": "ASC"}}}, 280, 50, 600)
        result = resolve_ordering("latest items", "records", (
            Column("name", "TEXT", True, False),
            Column("created", "TEXT", True, False)), client)
        self.assertEqual((result.column, result.direction, result.jev_calls),
                         ("created", "DESC", 1))
        _, questions = client.call.call_args.args
        self.assertEqual(len(questions), 5)
        self.assertIsNone(result.secondary_column)

    def test_secondary_ordering_shares_the_same_call(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c1"}, "direction": {"choice": "ASC"},
            "direction_explicit": {"noul": .95},
            "secondary_target": {"choice": "c0"},
            "secondary_direction": {"choice": "ASC"}}}, 300, 60, 650)
        result = resolve_ordering("order by city and then name", "people", (
            Column("name", "TEXT", True, False),
            Column("city", "TEXT", True, False)), client)
        self.assertEqual((result.column, result.direction), ("city", "ASC"))
        self.assertEqual((result.secondary_column, result.secondary_direction),
                         ("name", "ASC"))
        self.assertEqual(result.jev_calls, 1)

    def test_unspecified_direction_uses_sql_ascending_default(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c0"}, "direction": {"choice": "DESC"},
            "direction_explicit": {"noul": .08},
            "secondary_target": {"choice": "none"},
            "secondary_direction": {"choice": "ASC"}}}, 200, 30, 100)
        result = resolve_ordering("Sort the records by rating", "records", (
            Column("rating", "REAL", True, False),), client)
        self.assertEqual(result.direction, "ASC")


    def test_explicit_order_clause_ignores_earlier_extremum_word(self):
        client = Mock()
        result = resolve_ordering(
            "Show the latest episode in every season, ordered by season",
            "episodes",
            (Column("season", "INTEGER", True, False),
             Column("air_date", "TEXT", True, False)),
            client)
        self.assertEqual((result.column, result.direction, result.source),
                         ("season", "ASC", "explicit_order_clause"))
        client.call.assert_not_called()


    def test_explicit_compound_order_clause_keeps_secondary_key_without_jev(self):
        client = Mock()
        result = resolve_ordering(
            "Sort by height descending, then last name alphabetically.",
            "players",
            (Column("height", "INTEGER", True, False),
             Column("last_name", "TEXT", True, False)),
            client)
        self.assertEqual((result.column, result.direction), ("height", "DESC"))
        self.assertEqual((result.secondary_column, result.secondary_direction),
                         ("last_name", "ASC"))
        client.call.assert_not_called()

    def test_explicit_then_without_comma_stays_mechanical(self):
        client = Mock()
        result = resolve_ordering(
            "Sort by height descending then last name alphabetically.",
            "players",
            (Column("name", "TEXT", True, False),
             Column("height", "INTEGER", True, False),
             Column("last_name", "TEXT", True, False)),
            client)
        self.assertEqual((result.column, result.direction), ("height", "DESC"))
        self.assertEqual((result.secondary_column, result.secondary_direction),
                         ("last_name", "ASC"))
        client.call.assert_not_called()

    def test_explicit_secondary_clause_overrides_missed_jev_tie_order(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c0"}, "direction": {"choice": "DESC"},
            "direction_explicit": {"noul": .95},
            "secondary_target": {"choice": "none"},
            "secondary_direction": {"choice": "DESC"}}}, 200, 30, 100)
        result = resolve_ordering(
            "Show the tallest players, then last name alphabetically.",
            "players",
            (Column("height", "INTEGER", True, False),
             Column("last_name", "TEXT", True, False)),
            client)
        self.assertEqual((result.secondary_column, result.secondary_direction),
                         ("last_name", "ASC"))

    def test_ungrounded_ranking_can_choose_none_and_fail_closed(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "none", "probabilities": {"none": .91}},
            "direction": {"choice": "DESC"},
            "direction_explicit": {"noul": .9},
            "secondary_target": {"choice": "none"},
            "secondary_direction": {"choice": "ASC"}}}, 200, 30, 100)
        result = resolve_ordering("Show the happiest schools", "schools", (
            Column("name", "TEXT", True, False),
            Column("city", "TEXT", True, False)), client)
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.column)
        _, questions = client.call.call_args.args
        self.assertIn("none", questions["target"]["criteria"])

    def test_empty_schema_fails_closed(self):
        result = resolve_ordering("latest", "records", ())
        self.assertEqual(result.status, "ambiguous")

    def test_direction_review_receives_fixed_column_and_can_reverse_it(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "direction": {"choice": "ASC"}}}, 60, 5, 100)
        result = review_order_direction(
            "youngest animal", "animals", Column("age", "INTEGER", True, False),
            "DESC", client)
        self.assertEqual((result.direction, result.status), ("ASC", "resolved"))
        state, questions = client.call.call_args.args
        self.assertEqual(state["established_order_column"], "animals.age")
        self.assertEqual(set(questions["direction"]["criteria"]),
                         {"ASC", "DESC", "ambiguous"})


if __name__ == "__main__":
    unittest.main()
