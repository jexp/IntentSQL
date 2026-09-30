"""A schema-aware output refinement stays separate from SQL compilation."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.schema import Column
from intentsql.skills.stored_output import resolve_stored_output


class StoredOutputTests(unittest.TestCase):
    def test_only_selected_schema_columns_are_sent(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"stored": {"noul": .93}}}, 80, 20, 200)
        result = resolve_stored_output("Show the existing numeric attribute", {
            "records": (Column("amount", "INTEGER", True, False),)}, client)
        self.assertTrue(result.stored)
        state, questions = client.call.call_args.args
        self.assertEqual(state["inspected_columns"], {"records": ["amount"]})
        self.assertEqual(tuple(questions), ("stored",))

    def test_borderline_score_gets_focused_stored_vs_count_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"stored": {"noul": .50}}}, 80, 20, 200),
            CallResult({"answers": {"kind": {"choice": "stored",
                                              "confidence": .88,
                                              "probabilities": {"stored": .9,
                                                                "computed_count": .08,
                                                                "ambiguous": .02}}}},
                       90, 22, 210),
        ]
        result = resolve_stored_output(
            "Give me each district with its pupil count.",
            {"districts": (Column("id", "INTEGER", True, False),),
             "expenditures": (Column("pupils", "INTEGER", False, False),)},
            client)
        self.assertTrue(result.stored)
        self.assertEqual(result.jev_calls, 2)
        review_state = client.call.call_args_list[1].args[0]
        self.assertIn("expenditures.pupils", review_state["inspected_numeric_fields"])

    def test_borderline_score_can_confirm_computed_row_count(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"stored": {"noul": .45}}}, 80, 20, 200),
            CallResult({"answers": {"kind": {"choice": "computed_count"}}},
                       90, 22, 210),
        ]
        result = resolve_stored_output(
            "How many records are there?",
            {"records": (Column("amount", "INTEGER", False, False),)}, client)
        self.assertFalse(result.stored)
        self.assertEqual(result.status, "resolved")
