"""Missing-filter repair is bounded to inspected NULL capabilities."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.filter_repair import resolve_missing_null_filter
from intentsql.skills.schema import Column


class FilterRepairTests(unittest.TestCase):
    def test_can_add_non_null_on_already_bound_nullable_column(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"repair": {
            "choice": "c0_present", "confidence": .94,
            "probabilities": {"c0_present": .96, "none": .04},
        }}}, 30, 5, 2)
        result = resolve_missing_null_filter(
            "exclude rows whose category is missing",
            (Column("category", "TEXT", True, False),),
            ({"column": "category", "operator": "!=", "value": "X"},), client)
        self.assertEqual((result.column, result.operator, result.status),
                         ("category", "IS NOT NULL", "resolved"))

    def test_never_exposes_unmentioned_unrepresented_columns(self):
        client = Mock()
        result = resolve_missing_null_filter(
            "show ordinary rows",
            (Column("secret", "TEXT", True, False),), (), client)
        self.assertEqual(result.status, "unresolved")
        client.call.assert_not_called()


    def test_coverage_repair_can_semantically_ground_nullable_column_alias(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"repair": {
            "choice": "c2_present", "confidence": .94,
            "probabilities": {"c2_present": .94, "none": .06},
        }}}, 45, 7, 3)
        columns = (
            Column("id", "INTEGER", True, True),
            Column("first_name", "TEXT", True, False),
            Column("last_name", "TEXT", True, False),
            Column("bats", "TEXT", True, False),
            Column("throws", "TEXT", True, False),
        )
        result = resolve_missing_null_filter(
            "How many players have a batting side recorded?",
            columns, (), client, allow_semantic_column_fallback=True)
        self.assertEqual((result.column, result.operator, result.status),
                         ("bats", "IS NOT NULL", "resolved"))
        state, questions = client.call.call_args.args
        self.assertTrue(state["semantic_column_fallback"])
        self.assertNotIn("id", state["candidate_nullable_columns"])
        self.assertIn("bats", state["candidate_nullable_columns"])
        self.assertIn("none", questions["repair"]["criteria"])

    def test_low_confidence_repair_fails_closed(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"repair": {
            "choice": "c0_null", "confidence": .4,
        }}}, 30, 5, 2)
        result = resolve_missing_null_filter(
            "rows with missing category",
            (Column("category", "TEXT", True, False),), (), client)
        self.assertEqual(result.status, "unresolved")


if __name__ == "__main__":
    unittest.main()
