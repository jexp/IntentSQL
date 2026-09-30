"""Negative relationship existence is distinguished from NULL on an existing joined row."""

import unittest
from types import SimpleNamespace

from intentsql.skills.relation_absence import (
    resolve_relation_absence,
    should_probe_relation_absence,
)
from intentsql.skills.schema import Column, Relation


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def call(self, state, questions):
        self.calls.append((state, questions))
        return SimpleNamespace(
            answers={"relation_absence": self.answer},
            input_tokens=31, output_tokens=7, elapsed_ms=3,
        )


class RelationAbsenceTests(unittest.TestCase):
    def setUp(self):
        self.relation = Relation("salaries", "player_id", "players", "id")
        self.players = (
            Column("id", "INTEGER", True, True),
            Column("first_name", "TEXT", True, False),
        )
        self.salaries = (
            Column("id", "INTEGER", True, True),
            Column("player_id", "INTEGER", True, False),
            Column("salary", "INTEGER", True, False),
        )

    def test_generic_negative_existence_cues_only_trigger_review(self):
        self.assertTrue(should_probe_relation_absence(
            "Show players who never have a salary record."))
        self.assertTrue(should_probe_relation_absence(
            "Show customers without an order."))
        self.assertFalse(should_probe_relation_absence(
            "Show players with a salary record."))

    def test_anti_join_mode_is_resolved_as_unsupported_shape(self):
        client = FakeClient({
            "choice": "anti_join_absence",
            "confidence": .98,
            "probabilities": {"anti_join_absence": .98,
                              "joined_row_null": .01,
                              "ordinary_relation": .01},
        })
        result = resolve_relation_absence(
            "Show players who never have a salary record.",
            "players", "salaries", self.relation,
            self.players, self.salaries, client)
        self.assertEqual((result.mode, result.status),
                         ("anti_join_absence", "resolved"))
        self.assertEqual(client.calls[0][0]["related_table"], "salaries")

    def test_existing_related_row_with_null_field_can_continue(self):
        client = FakeClient({
            "choice": "joined_row_null",
            "confidence": .94,
            "probabilities": {"anti_join_absence": .03,
                              "joined_row_null": .94,
                              "ordinary_relation": .03},
        })
        result = resolve_relation_absence(
            "Show players whose salary record has no salary value.",
            "players", "salaries", self.relation,
            self.players, self.salaries, client)
        self.assertEqual((result.mode, result.status),
                         ("joined_row_null", "resolved"))

    def test_low_confidence_negative_relation_review_fails_closed(self):
        client = FakeClient({"choice": "ordinary_relation", "confidence": .41})
        result = resolve_relation_absence(
            "Show players without a salary record.",
            "players", "salaries", self.relation,
            self.players, self.salaries, client)
        self.assertEqual(result.status, "ambiguous")


if __name__ == "__main__":
    unittest.main()
