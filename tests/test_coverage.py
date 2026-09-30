"""A plausible SQL query must not execute when it drops a requested requirement."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from intentsql.database import connect
from intentsql.jev_client import CallResult, JevClient
from intentsql.semantic_read import run_read
from intentsql.skills.coverage import resolve_coverage, review_unique_result_filter


class CoverageTests(unittest.TestCase):
    def test_global_metric_unit_is_not_an_extra_filter(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"missing_filter": {"noul": .42},
                                    "unit_matches_metric": {"noul": .79}}}, 40, 4, 1),
        ]
        program = {"joined_tables": ["metrics", "entities"],
                   "outputs": ["entities.name"], "filters": [],
                   "order_by": [{"key": "metrics.per_unit_cost", "direction": "ASC"}],
                   "global_extremum": {"table": "metrics", "column": "per_unit_cost",
                                        "function": "MIN", "ties": "preserve"}}
        result = resolve_coverage("Find the entity with the lowest cost for each unit",
                                  program, client)
        self.assertEqual((result.category, result.jev_calls), ("complete", 2))

    def test_global_extremum_does_not_erase_real_missing_filter(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"missing_filter": {"noul": .94}}}, 40, 4, 1),
        ]
        program = {"joined_tables": ["metrics", "entities"],
                   "outputs": ["entities.name"], "filters": [],
                   "global_extremum": {"table": "metrics", "column": "amount",
                                        "function": "MIN", "ties": "preserve"}}
        result = resolve_coverage("Find the lowest amount among entities in the west",
                                  program, client)
        self.assertEqual(result.status, "unsupported")

    def test_constant_source_value_can_satisfy_redundant_filter_wording(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"invariants_cover": {"noul": .91}}}, 40, 4, 1),
        ]
        program = {"joined_tables": ["records"], "outputs": ["name"],
                   "filters": [{"column": "type", "operator": "!=", "value": "excluded"}],
                   "source_invariants": {"region_code": "R1"}}
        result = resolve_coverage("records in the named region excluding one type", program, client)
        self.assertEqual((result.category, result.jev_calls), ("complete", 2))

    def test_established_order_and_limit_get_narrow_coverage_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_order_limit"}}}, 80, 8, 1),
            CallResult({"answers": {"order_limit_coverage": {
                "choice": "complete", "probabilities": {"complete": .94}}}}, 40, 4, 1),
        ]
        program = {
            "joined_tables": ["players"],
            "outputs": ["first_name", "last_name", "birth_year"],
            "filters": [{"column": "birth_year", "operator": ">", "value": 1950}],
            "order_by": [
                {"key": "birth_year", "direction": "ASC"},
                {"key": "last_name", "direction": "ASC"},
            ],
            "limit": 7,
        }
        result = resolve_coverage(
            "List the first name, last name and birth year of the seven oldest players "
            "born after 1950, oldest first, then last name.",
            program, client)
        self.assertEqual((result.category, result.status, result.jev_calls),
                         ("complete", "resolved", 2))
        state, questions = client.call.call_args_list[1].args
        self.assertEqual(state["established_limit"], 7)
        self.assertEqual(state["established_order_by"][1]["key"], "last_name")
        self.assertIn("order_limit_coverage", questions)

    def test_narrow_order_limit_review_preserves_real_missing_requirement(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_order_limit"}}}, 80, 8, 1),
            CallResult({"answers": {"order_limit_coverage": {
                "choice": "missing_order_limit"}}}, 40, 4, 1),
        ]
        program = {
            "joined_tables": ["players"],
            "outputs": ["name"],
            "filters": [],
            "order_by": [{"key": "score", "direction": "DESC"}],
            "limit": 5,
        }
        result = resolve_coverage(
            "Show five players by score, then break ties alphabetically by name.",
            program, client)
        self.assertEqual((result.category, result.status, result.jev_calls),
                         ("missing_order_limit", "unsupported", 2))

    def test_tie_preserving_global_extremum_needs_no_limit(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_order_limit"}}}, 80, 8, 1),
            CallResult({"answers": {"extremum_complete": {"noul": .94}}}, 40, 4, 1),
        ]
        program = {"joined_tables": ["facts", "entities"], "outputs": ["entities.name"],
                   "filters": [], "order_by": [{"key": "facts.metric", "direction": "ASC"}],
                   "limit": None, "global_extremum": {"table": "facts", "column": "metric",
                                                        "function": "MIN", "ties": "preserve"}}
        self.assertEqual(resolve_coverage("entity with smallest metric; preserve ties",
                                          program, client).status, "resolved")

    def test_grounded_stored_pattern_can_complete_filter_coverage(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"pattern_complete": {"noul": .9}}}, 40, 4, 1),
        ]
        program = {"joined_tables": ["entities"], "outputs": ["name"],
                   "filters": [{"column": "name", "operator": "CONTAINS", "value": "(inactive)"}],
                   "grounded_pattern_bindings": [{"column": "name", "operator": "CONTAINS",
                                                    "value": "(inactive)"}]}
        self.assertEqual(resolve_coverage("entities no longer active", program, client).status,
                         "resolved")

    def test_grounded_code_binding_gets_narrow_missing_filter_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"missing_filter": {"noul": .12}}}, 40, 4, 1),
        ]
        program = {"joined_tables": ["facts", "entities"],
                   "outputs": ["facts.amount"],
                   "filters": [{"table": "entities", "column": "region",
                                "operator": "=", "value": "P.R."}],
                   "grounded_value_bindings": [{"table": "entities",
                        "column": "region", "stored_value": "P.R.",
                        "request_evidence": {"kind": "request_initialism",
                                             "request_phrases": ("puero rico",)}}]}
        result = resolve_coverage("facts from Puero Rico", program, client)
        self.assertEqual((result.category, result.jev_calls), ("complete", 2))
        self.assertIn("grounded_value_bindings", client.call.call_args.args[0])

    def test_grounded_binding_does_not_erase_real_missing_filter(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"missing_filter": {"noul": .91}}}, 40, 4, 1),
        ]
        program = {"joined_tables": ["facts", "entities"],
                   "filters": [{"column": "region", "operator": "=", "value": "P.R."}],
                   "grounded_value_bindings": [{"column": "region",
                                                "stored_value": "P.R."}]}
        self.assertEqual(resolve_coverage("facts from Puero Rico over 100", program,
                                          client).status, "unsupported")


    def test_grouped_no_filter_gets_narrow_review_before_rejection(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"grouped_filter_coverage": {
                "choice": "complete",
                "confidence": .91,
                "probabilities": {"complete": .95, "missing_filter": .05}}}},
                       45, 5, 1),
        ]
        program = {
            "joined_tables": ["schools"],
            "outputs": ["type", "COUNT(*)"],
            "filters": [],
            "groups": [{"group by": "type"}],
            "having": [],
            "order_by": [{"key": "row_count", "direction": "DESC"}],
            "limit": None,
        }
        result = resolve_coverage(
            "List each school type and its number of schools, largest count first.",
            program, client)
        self.assertEqual((result.category, result.status, result.jev_calls),
                         ("complete", "resolved", 2))
        state, questions = client.call.call_args_list[1].args
        self.assertIn("established_groups", state)
        self.assertIn("grouped_filter_coverage", questions)

    def test_grouped_review_preserves_real_source_filter(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_filter"}}}, 80, 8, 1),
            CallResult({"answers": {"grouped_filter_coverage": {
                "choice": "missing_filter"}}}, 45, 5, 1),
        ]
        program = {
            "joined_tables": ["schools"],
            "outputs": ["city", "COUNT(*)"],
            "filters": [],
            "groups": [{"group by": "city"}],
            "having": [],
            "order_by": [],
            "limit": None,
        }
        result = resolve_coverage(
            "Count public schools by city.", program, client)
        self.assertEqual((result.category, result.status),
                         ("missing_filter", "unsupported"))

    def test_unique_matched_row_can_clear_only_a_spurious_filter_objection(self):
        client = Mock()
        client.call.return_value = CallResult(
            {"answers": {"filter_coverage": {"choice": "complete"}}}, 50, 4, 1)
        result = review_unique_result_filter(
            "find the holiday episode aired on December 31st, 2004",
            {"filters": [{"column": "air_date", "operator": "=",
                          "value": "2004-12-31"}]},
            {"title": "Starlight Night", "air_date": "2004-12-31"}, client)
        self.assertEqual(result.status, "resolved")
        state, questions = client.call.call_args.args
        self.assertIn("unique_matching_source_row", state)
        self.assertEqual(questions["filter_coverage"]["type"], "choice")

    def test_one_table_relation_objection_gets_independent_schema_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "unsupported_relation"}}}, 100, 10, 1),
            CallResult({"answers": {"relationship_coverage": {
                "choice": "local_complete"}}}, 60, 7, 1),
        ]
        program = {"joined_tables": ["records"], "outputs": ["AVG(amount)"],
                   "filters": [], "order_by": [], "limit": None}
        result = resolve_coverage("average recorded amount", program, client,
                                  source_columns=("amount", "id"))
        self.assertEqual((result.category, result.jev_calls), ("complete", 2))
        state, _ = client.call.call_args_list[1].args
        self.assertEqual(state["source_columns"], ("amount", "id"))

    def test_real_missing_relation_still_rejects(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "unsupported_relation"}}}, 100, 10, 1),
            CallResult({"answers": {"relationship_coverage": {
                "choice": "missing_relation"}}}, 60, 7, 1),
        ]
        result = resolve_coverage("show parent name", {"joined_tables": ["children"],
            "outputs": ["parent_id"], "filters": []}, client,
            source_columns=("parent_id",))
        self.assertEqual(result.status, "unsupported")

    def test_validated_direct_join_gets_focused_relation_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "unsupported_relation"}}}, 80, 8, 1),
            CallResult({"answers": {"direct_join_complete": {"noul": .92}}}, 50, 5, 1),
        ]
        program = {"joined_tables": ["expenditures", "districts"],
                   "joins": [{"join": "districts", "on": "expenditures.district_id = districts.id"}],
                   "outputs": ["districts.name", "expenditures.pupils"],
                   "filters": [], "order_by": []}
        result = resolve_coverage("districts with pupil count", program, client)
        self.assertEqual((result.status, result.jev_calls), ("resolved", 2))

    def test_validated_direct_join_accepts_a_positive_but_uncertain_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "unsupported_relation"}}}, 80, 8, 1),
            CallResult({"answers": {"direct_join_complete": {"noul": .57}}}, 50, 5, 1),
        ]
        program = {"joined_tables": ["child", "parent"],
                   "joins": [{"join": "parent", "on": "child.parent_id = parent.id"}],
                   "outputs": ["parent.name", "child.amount"], "filters": []}
        self.assertEqual(resolve_coverage("parent and amount", program, client).status,
                         "resolved")

    def test_bounded_question_uses_compact_plan_and_no_sql(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_output",
                                                "confidence": .94}}}, 300, 20, 40),
            CallResult({"answers": {"output_coverage": {"choice": "c0"}}}, 80, 5, 20),
        ]
        program = {"joined_tables": ["items"], "outputs": ["name"],
                   "filters": [], "typed_query": {"unnecessary": "detail"}}
        coverage = resolve_coverage(
            "show name and amount", program, client,
            source_columns=("name", "amount"))
        self.assertEqual(coverage.status, "unsupported")
        state, questions = client.call.call_args_list[0].args
        self.assertNotIn("typed_query", state["compiled_plan"])
        self.assertEqual(questions["coverage"]["type"], "choice")
        self.assertEqual(coverage.input_tokens, 380)

    def test_paraphrased_output_requires_independent_review_of_other_requirements(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_output"}}}, 100, 10, 1),
            CallResult({"answers": {"output_coverage": {"choice": "none"}}}, 40, 5, 1),
            CallResult({"answers": {"coverage": {"choice": "complete"}}}, 80, 8, 1),
        ]
        result = resolve_coverage("show country code and count",
            {"joined_tables": ["players"], "outputs": ["birth_country", "COUNT(*)"],
             "groups": [{"group by": "birth_country"}]}, client)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.jev_calls, 3)
        self.assertEqual(len(result.trace), 3)

    def test_output_review_receives_lexical_schema_and_per_group_evidence(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {"choice": "missing_output"}}}, 10, 2, 1),
            CallResult({"answers": {"output_coverage": {"choice": "c0"}}}, 8, 1, 1),
        ]
        program = {
            "joined_tables": ["episodes"],
            "outputs": ["season", "title", "air_date"],
            "filters": [],
            "per_group_extremum": {
                "group_column": "season", "extremum_column": "air_date",
                "function": "MIN", "ties": "preserve",
            },
        }
        resolve_coverage(
            "show the first episode aired in every season with title and air date",
            program, client,
            source_columns=("season", "episode_in_season", "title", "air_date"))
        state = client.call.call_args_list[1].args[0]
        self.assertEqual(state["explicit_schema_mentions"],
                         ("season", "title", "air_date"))
        self.assertEqual(state["per_group_row_selector"]["extremum_column"], "air_date")
        self.assertIn("filters", state["non_output_uses"])

    def test_partial_program_is_rejected_before_compiled_or_execute_event(self):
        route = {name: {"noul": 0} for name in (
            "filters", "ordering", "distinct", "grouping", "relationship",
            "having", "expression", "windowing")}
        route.update(output={"choice": "rows"}, quantity={"choice": "numeric"})
        responses = [
            {"answers": route, "usage": {"input_tokens": 10, "output_tokens": 2}},
            {"answers": {"coverage": {"choice": "missing_filter"}},
             "usage": {"input_tokens": 8, "output_tokens": 1}},
            {"answers": {"repair": {"choice": "none", "confidence": .99}},
             "usage": {"input_tokens": 6, "output_tokens": 1}},
        ]
        events = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "items.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE items (id INTEGER)")
                conn.execute("INSERT INTO items VALUES (1)")
            client = JevClient(api_key="")
            with patch("intentsql.jev_client.urlopen", side_effect=[
                    io.BytesIO(json.dumps(item).encode()) for item in responses]):
                with self.assertRaisesRegex(ValueError, "no partial query was executed"):
                    run_read(path, "show the first 2 rows with a required condition",
                             client, on_event=events.append)
        self.assertNotIn("compiled", [item["kind"] for item in events])
        self.assertNotIn("execute", [item.get("phase") for item in events])


    def test_pattern_predicate_is_not_reclassified_as_unsupported_expression(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"coverage": {
                "choice": "unsupported_expression"}}}, 80, 8, 1),
            CallResult({"answers": {"expression_coverage": {
                "choice": "complete", "probabilities": {"complete": .96}}}},
                40, 4, 1),
        ]
        program = {
            "joined_tables": ["episodes"],
            "outputs": ["title"],
            "filters": [{"column": "title", "operator": "PREFIX",
                         "value": "The", "table": None}],
            "filter_connector": "AND",
            "groups": [], "having": [],
            "order_by": [{"key": "title", "direction": "ASC"}],
            "limit": None, "distinct": "UNSET",
            "per_group_extremum": None,
            "relationship_scope": "local_only",
        }
        result = resolve_coverage(
            "Show episode titles beginning with The, alphabetically.",
            program, client, source_columns=("id", "title"))
        self.assertEqual(result.category, "complete")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.jev_calls, 2)

if __name__ == "__main__":
    unittest.main()
