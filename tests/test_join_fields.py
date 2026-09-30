"""Joined output fields carry table qualification from inspected schemas."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.join_fields import QualifiedField, resolve_join_fields
from intentsql.skills.schema import Column, Relation


class JoinFieldsTests(unittest.TestCase):
    def test_output_is_qualified_and_schema_grounded(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "f0": {"noul": .1}, "f1": {"noul": .9}, "f2": {"noul": .1}}},
            200, 40, 300)
        result = resolve_join_fields("names of related records", {
            "facts": (Column("id", "INT", False, True),),
            "people": (Column("name", "TEXT", True, False),
                       Column("id", "INT", False, True))}, client)
        self.assertEqual(result.fields, (QualifiedField("people", "name"),))

    def test_related_entity_prefers_human_facing_field_over_fk_id(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "f0": {"noul": .02}, "f1": {"noul": .05},
            "f2": {"noul": .03}, "f3": {"noul": .20},
            "entity_rows": {"noul": .96},
            "entity_label": {"choice": "f3", "confidence": .98,
                             "probabilities": {"f3": .98, "none": .02}},
        }}, 100, 15, 100)
        edge = Relation("expenditures", "district_id", "districts", "id")
        result = resolve_join_fields(
            "Which district has the lowest per-pupil expenditure?",
            {
                "expenditures": (Column("district_id", "INT", True, False),
                                 Column("per_pupil_expenditure", "NUMERIC", True, False)),
                "districts": (Column("id", "INT", True, True),
                              Column("name", "TEXT", True, False)),
            }, client, relation=edge)
        self.assertEqual(result.fields, (QualifiedField("districts", "name"),))
        state, _ = client.call.call_args.args
        self.assertEqual(state["foreign_key"]["child_column"], "district_id")
        self.assertIn("human-readable", state["task"])


if __name__ == "__main__":
    unittest.main()
