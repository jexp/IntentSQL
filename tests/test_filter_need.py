"""Filter refinement sees only the selected table's schema."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.filter_need import resolve_filter_need
from intentsql.skills.schema import Column


class FilterNeedTests(unittest.TestCase):
    def test_one_bounded_judgment(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "filter": {"noul": .9}}}, 100, 15, 250)
        result = resolve_filter_need("narrow subtype", "records", (
            Column("type", "TEXT", True, False),), client)
        self.assertTrue(result.needed)
        self.assertEqual(result.jev_calls, 1)

    def test_group_threshold_is_separate_from_source_filter(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"filter": {"noul": .1}}}, 75, 10, 100)
        result = resolve_filter_need("groups over threshold", "records", (
            Column("category", "TEXT", True, False),), client,
            group_measure_filter=True)
        self.assertFalse(result.needed)
        state, questions = client.call.call_args.args
        self.assertTrue(state["group_measure_filter"])
        self.assertIn("before grouping", questions["filter"]["instructions"])
        self.assertLess(len(questions["filter"]["instructions"]), 100)

    def test_grouped_filter_receives_exact_literals_and_typed_measure(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "filter": {"noul": .9}}}, 75, 10, 100)
        facts = ({"id": "integer:10:14", "kind": "integer", "value": 1998},
                 {"id": "integer:23:27", "kind": "integer", "value": 2000})
        result = resolve_filter_need("range request", "performances", (
            Column("year", "INTEGER", True, False),
            Column("HR", "INTEGER", True, False)), client,
            literal_facts=facts, established_groups=("year",),
            established_measure="SUM of HR per group")
        self.assertTrue(result.needed)
        state = client.call.call_args.args[0]
        self.assertEqual(state["exact_request_literals"], facts)
        self.assertEqual(state["established_group_keys"], ("year",))
        self.assertEqual(state["established_computed_measure"],
                         "SUM of HR per group")

    def test_grouped_filter_uses_retrieved_category_evidence(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "filter_column": {"choice": "c0"}}}, 75, 10, 100)
        result = resolve_filter_need(
            "count groups except the excluded category", "records", (
                Column("category", "TEXT", True, False),
                Column("amount", "INTEGER", True, False)), client,
            category_hints={"category": ("AA", "BB")},
            established_groups=("category",),
            established_measure="count of rows")
        self.assertTrue(result.needed)
        state, questions = client.call.call_args.args
        self.assertEqual(state["small_category_values"]["category"], ("AA", "BB"))
        self.assertEqual(questions["filter_column"]["type"], "choice")

    def test_grouped_filter_uses_targeted_group_value_evidence(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "filter_column": {"choice": "c0"}}}, 75, 10, 100)
        result = resolve_filter_need(
            "exclude the named group", "records", (
                Column("group_code", "TEXT", True, False),
                Column("amount", "INTEGER", True, False)), client,
            established_groups=("group_code",),
            candidate_value_evidence={"group_code": ("ZZ",)})
        self.assertTrue(result.needed)
        state, questions = client.call.call_args.args
        self.assertEqual(state["request_related_observed_values"],
                         {"group_code": ("ZZ",)})
        self.assertEqual(questions["filter_column"]["criteria"]["c0"]["column"],
                         "group_code")

    def test_duplicate_operand_review_receives_established_condition(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "additional": {"noul": .08}}}, 75, 10, 100)
        result = resolve_filter_need(
            "with category Sci", "records", (
                Column("category", "TEXT", True, False),
                Column("code", "TEXT", True, False)), client,
            relational_selector={"represented_predicate": {
                "column": "category", "operator": "PREFIX", "value": "Sci"}},
            duplicate_candidate={"column": "code", "operator": "PREFIX",
                                 "value": "Sci"})
        self.assertFalse(result.needed)
        state, questions = client.call.call_args.args
        self.assertEqual(state["candidate_additional_condition"]["column"], "code")
        self.assertEqual(questions["additional"]["type"], "noul")


if __name__ == "__main__":
    unittest.main()
