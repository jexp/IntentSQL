"""Focused Alpha regressions for row-count, filter and ranking role separation."""
import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.filter_need import resolve_filter_need
from intentsql.skills.group_clauses import resolve_group_clauses
from intentsql.skills.predicate_columns import refine_predicate_candidates
from intentsql.skills.result_cardinality import resolve_result_cardinality
from intentsql.skills.schema import Column
from intentsql.skills.quantity import resolve_quantity
from intentsql.skills.facts import extract_facts
from intentsql.skills.value_hints import targeted_values
from intentsql.skills.predicate_value import value_candidates
from intentsql.database import connect


def client_for(answers):
    client = Mock()
    client.call.return_value = CallResult({"answers": answers}, 20, 5, 1)
    return client


class AlphaUserShapeTests(unittest.TestCase):
    def test_order_only_tail_can_resolve_without_erasing_existing_predicate(self):
        client = client_for({"missing_condition": {"choice": "none"}})
        result = resolve_filter_need("first three entries in slot order within batch 4", "entries",
            (Column("batch", "INT", True, False), Column("slot", "INT", True, False)), client,
            relational_selector={"represented_predicates": ({"column": "batch", "operator": "=", "value": 4},),
                                 "unresolved_candidate_columns": ("slot",)})
        self.assertFalse(result.needed)
        state, questions = client.call.call_args.args
        self.assertEqual(set(questions["missing_condition"]["criteria"]), {"slot", "none"})
        self.assertEqual(state["already_represented_row_selector"]["represented_predicates"][0]["value"], 4)

    def test_real_additional_condition_still_blocks_a_partial_plan(self):
        client = client_for({"missing_condition": {"choice": "slot"}})
        result = resolve_filter_need("batch 4 and slot greater than 2", "entries",
            (Column("slot", "INT", True, False),), client,
            relational_selector={"unresolved_candidate_columns": ("slot",)})
        self.assertTrue(result.needed)

    def test_semantic_category_role_is_selected_only_from_inspected_columns(self):
        client = client_for({"column": {"choice": "origin", "confidence": .9}})
        result = refine_predicate_candidates("items of local origin", "objects",
            (Column("origin", "TEXT", True, False),), ("origin",), {"origin": ("LOC", "EXT")}, client)
        self.assertEqual(result.columns, ("origin",))
        criteria = client.call.call_args.args[1]["column"]["criteria"]
        self.assertEqual(criteria["origin"]["observed_values"], ("LOC", "EXT"))

    def test_uncertain_category_role_stays_ambiguous(self):
        client = client_for({"column": {"choice": "origin", "confidence": .4}})
        result = refine_predicate_candidates("unclear origin", "objects",
            (Column("origin", "TEXT", True, False),), ("origin",), {}, client)
        self.assertEqual((result.columns, result.status), ((), "ambiguous"))

    def test_output_count_is_not_a_group_measure_cutoff(self):
        client = client_for({"having": {"noul": .9}, "ordering": {"noul": .9},
                             "ordering_basis": {"choice": "measure"}, "threshold_fact": {"choice": "none"}})
        result = resolve_group_clauses("Return five batches with the highest peak value", "batch", "MAX(value)", client)
        self.assertFalse(result.having)
        self.assertTrue(result.ordering)

    def test_resolved_quantity_owns_its_exact_fact(self):
        client = client_for({"limit": {"choice": "w0"}})
        request = "Return three entries from batch 4"
        result = resolve_quantity(request, "worded", client, other_numeric_roles=True)
        self.assertEqual(result.value, 3)
        self.assertEqual(result.fact_id, next(f.fact_id for f in extract_facts(request) if f.value == 3))

    def test_limit_fact_cannot_become_a_having_cutoff(self):
        request = "Return five batches with the highest peak value"
        fact = next(f for f in extract_facts(request) if f.value == 5)
        client = client_for({"having": {"noul": .99}, "ordering": {"noul": .9},
                             "ordering_basis": {"choice": "measure"}, "threshold_fact": {"choice": "none"}})
        result = resolve_group_clauses(request, "batch", "MAX(value)", client, excluded_fact_ids=(fact.fact_id,))
        self.assertFalse(result.having)
        self.assertEqual(client.call.call_args.args[0]["candidate_group_thresholds"], ())

    def test_common_category_survives_a_misleading_lexical_candidate(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE items (origin TEXT)")
            conn.executemany("INSERT INTO items VALUES (?)", [("LOC",)] * 8 + [("Local Islands",), ("EXT",)])
            values = targeted_values(conn, "items", "origin", "locally sourced items", include_common=True)
        self.assertIn("LOC", values)
        choices = value_candidates("local items", Column("origin", "TEXT", True, False),
                                   ("Local Islands", "LOC", "EXT"), (), "=")
        self.assertIn("LOC", {c["value"] for c in choices.values()})

    def test_established_extremum_cardinality_question_does_not_reask_sorting(self):
        client = client_for({"cardinality": {"choice": "top_one", "confidence": .95}})
        result = resolve_result_cardinality("the smallest entity and its value",
            {"column": "value", "direction": "ASC"}, client, tie_preserving_extremum=True)
        self.assertEqual((result.mode, result.status), ("top_one", "resolved"))
        self.assertIn("already selects", client.call.call_args.args[0]["task"])
