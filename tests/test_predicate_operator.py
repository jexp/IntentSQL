"""Operator choices depend on inspected SQL type, not database names."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.predicate import resolve_predicate_operator
from intentsql.skills.schema import Column


class PredicateOperatorTests(unittest.TestCase):
    def test_text_can_choose_contains(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"operator": {
            "choice": "CONTAINS", "confidence": .9,
            "probabilities": {"CONTAINS": .94}}}}, 330, 50, 620)
        result = resolve_predicate_operator("items about fractions",
                    Column("subject", "TEXT", True, False), client)
        self.assertEqual(result.operator, "CONTAINS")
        _, questions = client.call.call_args.args
        self.assertIn("CONTAINS", questions["operator"]["criteria"])

    def test_numeric_does_not_offer_text_matching(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"operator": {
            "choice": ">", "confidence": .9, "probabilities": {">": .9}}}},
            330, 40, 620)
        resolve_predicate_operator("weight above 30",
                    Column("weight", "REAL", True, False), client)
        _, questions = client.call.call_args.args
        self.assertNotIn("CONTAINS", questions["operator"]["criteria"])

    def test_iso_date_text_column_does_not_offer_text_matching(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"operator": {
            "choice": "=", "confidence": .95, "probabilities": {"=": .95}}}},
            220, 25, 400)
        result = resolve_predicate_operator(
            "How many purchases were placed in 2026?",
            Column("order_date", "TEXT", True, False), client,
            date_semantics=True)
        self.assertEqual(result.operator, "=")
        state, questions = client.call.call_args.args
        self.assertEqual(state["semantic_type"], "iso_date")
        self.assertNotIn("CONTAINS", questions["operator"]["criteria"])
        self.assertNotIn("PREFIX", questions["operator"]["criteria"])
        self.assertNotIn("SUFFIX", questions["operator"]["criteria"])

    def test_close_operator_scores_get_targeted_refinement(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"operator": {"choice": ">=",
                "probabilities": {">=": .49, "=": .47}}}}, 200, 30, 400),
            CallResult({"answers": {"operator": {"choice": "=",
                "probabilities": {"=": .9, ">=": .1}}}}, 90, 15, 200),
        ]
        result = resolve_predicate_operator("items in group 2 or category A",
            Column("group", "INTEGER", True, False), client)
        self.assertEqual((result.operator, result.jev_calls), ("=", 2))

    def test_complete_observed_value_is_checked_before_wildcard_matching(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"operator": {"choice": "CONTAINS",
                "probabilities": {"CONTAINS": .8, "=": .1}}}}, 100, 12, 100),
            CallResult({"answers": {"match_scope": {"choice": "exact"}}}, 70, 8, 100),
        ]
        result = resolve_predicate_operator("records with status Active",
            Column("status", "TEXT", True, False), client,
            value_hints=("Active", "Inactive"))
        self.assertEqual((result.operator, result.jev_calls), ("=", 2))
        state, _ = client.call.call_args_list[1].args
        self.assertEqual(state["complete_observed_values_in_request"], ("Active",))

    def test_explicit_partial_match_remains_partial(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"operator": {"choice": "PREFIX",
                "probabilities": {"PREFIX": .8, "=": .1}}}}, 100, 12, 100),
            CallResult({"answers": {"match_scope": {"choice": "partial"}}}, 70, 8, 100),
        ]
        result = resolve_predicate_operator("labels beginning with West",
            Column("label", "TEXT", True, False), client, value_hints=("West",))
        self.assertEqual(result.operator, "PREFIX")

    def test_scope_review_receives_related_observed_text_values(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"operator": {"choice": "=",
                "probabilities": {"=": .45, "CONTAINS": .43}},
                "partial_scope": {"noul": .7}}}, 100, 12, 100),
            CallResult({"answers": {"operator": {"choice": "CONTAINS"}}}, 40, 5, 30),
            CallResult({"answers": {"match_scope": {"choice": "partial"}}}, 40, 5, 30),
        ]
        result = resolve_predicate_operator(
            "episodes about fractions", Column("topic", "TEXT", True, False), client,
            value_hints=("Fractions", "Equivalent Fractions", "Fractions 101"))
        self.assertEqual(result.operator, "CONTAINS")
        state, _ = client.call.call_args_list[2].args
        self.assertEqual(set(state["other_observed_values_containing_that_text"]),
                         {"Equivalent Fractions", "Fractions 101"})

    def test_established_equality_is_not_widened_by_related_values(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"operator": {
            "choice": "=", "probabilities": {"=": .94}}}}, 100, 12, 100)
        result = resolve_predicate_operator("items in the Amber category",
            Column("category", "TEXT", True, False), client,
            value_hints=("Amber", "Dark Amber", "Light Amber"))
        self.assertEqual((result.operator, result.jev_calls), ("=", 1))
        self.assertEqual(client.call.call_count, 1)

    def test_unresolved_scope_is_not_executed_as_a_wildcard(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"operator": {"choice": "CONTAINS",
                "probabilities": {"CONTAINS": .9}}}}, 100, 12, 100),
            CallResult({"answers": {"match_scope": {"choice": "ambiguous"}}}, 40, 5, 30),
        ]
        result = resolve_predicate_operator("Amber entries",
            Column("category", "TEXT", True, False), client, value_hints=("Amber", "Dark Amber"))
        self.assertEqual(result.status, "ambiguous")


if __name__ == "__main__":
    unittest.main()
