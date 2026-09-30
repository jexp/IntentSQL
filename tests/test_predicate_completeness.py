"""Implicit categorical modifiers are recovered only from bounded observed values."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.predicate_completeness import (
    category_modifier_candidates, resolve_predicate_completion,
)


class PredicateCompletenessTests(unittest.TestCase):
    def test_discriminative_category_word_is_candidate(self):
        hints = {"type": ("Public School", "Charter School")}
        self.assertEqual(
            category_modifier_candidates(
                "How many public schools are located in Boston?", hints),
            (("type", "Public School"),),
        )
        self.assertEqual(
            category_modifier_candidates(
                "How many charter schools are located in Boston?", hints),
            (("type", "Charter School"),),
        )

    def test_shared_entity_word_does_not_create_candidate(self):
        hints = {"type": ("Public School", "Charter School")}
        self.assertEqual(
            category_modifier_candidates("How many schools are in Boston?", hints), ())

    def test_already_represented_column_is_not_reconsidered(self):
        hints = {"type": ("Public School", "Charter School")}
        self.assertEqual(
            category_modifier_candidates(
                "public schools in Boston", hints, ("type",)), ())

    def test_jev_must_confirm_schema_grounded_candidate(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"missing_category": {
            "choice": "c0_include", "confidence": .96,
            "probabilities": {"c0_include": .98, "none": .02},
        }}}, 180, 35, 250)
        result = resolve_predicate_completion(
            "How many public schools are located in Boston?", "schools",
            {"type": ("Public School", "Charter School")},
            ({"column": "city", "operator": "=", "value": "Boston"},), client)
        self.assertEqual((result.column, result.value), ("type", "Public School"))
        self.assertEqual(result.jev_calls, 1)
        state, question = client.call.call_args.args
        self.assertEqual(state["already_represented_predicates"][0]["column"], "city")
        self.assertIn("none", question["missing_category"]["criteria"])


    def test_negative_modifier_can_exclude_observed_category(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"missing_category": {
            "choice": "c0_exclude", "confidence": .97,
            "probabilities": {"c0_exclude": .98, "c0_include": .01, "none": .01},
        }}}, 180, 35, 250)
        result = resolve_predicate_completion(
            "Worcester schools, but leave out the charter ones.", "schools",
            {"type": ("Charter School", "Public School")},
            ({"column": "city", "operator": "=", "value": "Worcester"},), client)
        self.assertEqual((result.column, result.operator, result.value),
                         ("type", "!=", "Charter School"))

    def test_low_confidence_candidate_is_not_added(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"missing_category": {
            "choice": "c0_include", "confidence": .51,
            "probabilities": {"c0_include": .55, "none": .45},
        }}}, 180, 35, 250)
        result = resolve_predicate_completion(
            "public schools", "schools",
            {"type": ("Public School", "Charter School")}, (), client)
        self.assertIsNone(result.column)
        self.assertIsNone(result.value)


if __name__ == "__main__":
    unittest.main()
