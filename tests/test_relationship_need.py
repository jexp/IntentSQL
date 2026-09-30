"""Relationship refinement uses the selected table's actual columns."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.relationship_need import resolve_relationship_need
from intentsql.skills.schema import Column


class RelationshipNeedTests(unittest.TestCase):
    def test_one_schema_aware_check(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "other_table": {"noul": .05}}}, 100, 15, 200)
        result = resolve_relationship_need("count by city", "schools", (
            Column("city", "TEXT", True, False),), client)
        self.assertFalse(result.needs_other_table)
        state, _ = client.call.call_args.args
        self.assertEqual(state["available_columns"], ["city"])

    def test_fk_context_marks_related_entity_label_as_cross_table_fact(self):
        client = Mock()
        client.call.side_effect = [CallResult({"answers": {
            "scope": {"choice": "direct_relation_required", "confidence": .96,
                      "probabilities": {"direct_relation_required": .96, "local_only": .04}}
        }}, 100, 15, 200), CallResult({"answers": {
            "local_sufficient": {"noul": .05}}}, 70, 8, 150)]
        related = {
            "districts": {
                "columns": ("id", "name", "city"),
                "foreign_key": {
                    "child_table": "expenditures", "child_column": "district_id",
                    "parent_table": "districts", "parent_column": "id",
                },
            }
        }
        result = resolve_relationship_need(
            "Which district has the lowest per-pupil expenditure?",
            "expenditures",
            (Column("district_id", "INT", True, False),
             Column("per_pupil_expenditure", "NUMERIC", True, False)),
            client, related_schema=related)
        self.assertTrue(result.needs_other_table)
        self.assertEqual(result.jev_calls, 2)
        state, question = client.call.call_args_list[0].args
        self.assertEqual(state["inspected_direct_relationships"], related)
        self.assertIn("direct relation", question["scope"]["instructions"])
        self.assertLess(len(question["scope"]["instructions"]), 100)

    def test_independent_local_sufficiency_can_cancel_false_join(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"scope": {"choice": "direct_relation_required"}}}, 90, 12, 100),
            CallResult({"answers": {"local_sufficient": {"noul": .95}}}, 55, 8, 100),
        ]
        result = resolve_relationship_need("show each record's label", "records",
            (Column("label", "TEXT", True, False),), client,
            related_schema={"owners": {"columns": ("name",)}})
        self.assertFalse(result.needs_other_table)
        self.assertEqual(result.input_tokens, 145)
        state, _ = client.call.call_args_list[1].args
        self.assertNotIn("inspected_direct_relationships", state)


if __name__ == "__main__":
    unittest.main()
