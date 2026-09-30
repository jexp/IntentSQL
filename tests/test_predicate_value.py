"""Predicate values are bounded to request facts or inspected values."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.predicate_value import resolve_predicate_value, value_candidates
from intentsql.skills.schema import Column


class PredicateValueTests(unittest.TestCase):
    def test_natural_calendar_date_is_preserved_as_one_exact_operand(self):
        result = resolve_predicate_value(
            "aired on December 31st, 2004",
            Column("air_date", "TEXT", True, False), "=",
            date_semantics=True)
        self.assertEqual((result.value, result.jev_calls), ("2004-12-31", 0))

    def test_exact_quoted_equality_is_not_replaced_by_padded_sample(self):
        result = resolve_predicate_value(
            'departing from airport "APG"',
            Column("source_airport", "TEXT", True, False), "=",
            hints=(" APG",))
        self.assertEqual((result.value, result.source), ("APG", "request_literal"))

    def test_unquoted_exact_phrase_does_not_inherit_storage_padding(self):
        client = Mock()
        result = resolve_predicate_value(
            "flights into ATO", Column("destination", "TEXT", True, False), "=",
            hints=(" ATO",), client=client)
        self.assertEqual((result.value, result.source),
                         ("ATO", "observed_request_evidence"))
        client.call.assert_not_called()

    def test_single_numeric_literal_needs_no_call(self):
        result = resolve_predicate_value("salary above 300", Column("salary", "INT", True, False), ">")
        self.assertEqual((result.value, result.jev_calls), (300, 0))

    def test_sentence_punctuation_does_not_hide_integer(self):
        result = resolve_predicate_value("season 5. Show titles",
                                         Column("season", "INT", True, False), "=")
        self.assertEqual(result.value, 5)

    def test_multiple_numbers_are_chosen_by_jev(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"value": {
            "choice": "literal_1", "probabilities": {"literal_1": .9}}}}, 100, 20, 100)
        result = resolve_predicate_value("first 5 rows from season 6",
            Column("season", "INT", True, False), "=", client=client)
        self.assertEqual(result.value, 6)

    def test_semantic_numeric_value_is_bounded_to_inspected_candidates(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"value": {
            "choice": "observed_0", "confidence": .94,
            "probabilities": {"observed_0": .94, "none": .06}}}}, 80, 10, 20)
        result = resolve_predicate_value(
            "the original season", Column("season", "INTEGER", True, False), "=",
            hints=("1", "14"), client=client)
        self.assertEqual((result.value, result.source), (1, "observed_numeric_value"))
        state, questions = client.call.call_args.args
        self.assertNotIn("worded", questions["value"]["criteria"])
        self.assertEqual({item["value"] for item in questions["value"]["criteria"].values()},
                         {1, 14, None})

    def test_close_numeric_extremum_binding_gets_one_focused_refinement(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"value": {"choice": "none", "confidence": .5,
                "probabilities": {"none": .53, "observed_0": .47}}}}, 80, 10, 20),
            CallResult({"answers": {"value": {"choice": "observed",
                "probabilities": {"observed": .9, "none": .1}}}}, 40, 5, 10),
        ]
        result = resolve_predicate_value(
            "the original season", Column("season", "INTEGER", True, False), "=",
            hints=("1", "14"), client=client)
        self.assertEqual((result.value, result.jev_calls), (1, 2))

    def test_text_value_must_be_supplied(self):
        column = Column("topic", "TEXT", True, False)
        candidates = value_candidates("episodes about fractions", column,
                                      ("Fractions", "Equivalent Fractions"), ())
        self.assertEqual({item["value"] for item in candidates.values()},
                         {"Fractions", "Equivalent Fractions"})



    def test_substring_coincidence_does_not_hide_compact_code_candidate(self):
        column = Column("birth_country", "TEXT", True, False)
        candidates = value_candidates(
            "How many players were born in Canada?", column, ("Germany", "CAN"), ())
        values = {item["value"] for item in candidates.values()}
        self.assertEqual(values, {"Germany", "CAN"})
        can = next(item for item in candidates.values() if item["value"] == "CAN")
        self.assertEqual(can["evidence"]["kind"], "compact_observed_code_prefix")

    def test_compact_code_gets_focused_semantic_review_after_none(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"value": {
                "choice": "none", "confidence": .62,
                "probabilities": {"observed_0": .1, "observed_1": .28, "none": .62},
            }}}, 120, 20, 60),
            CallResult({"answers": {"equivalence": {
                "choice": "observed", "confidence": .96,
                "probabilities": {"observed": .96, "none": .04},
            }}}, 80, 10, 40),
        ]
        result = resolve_predicate_value(
            "How many players were born in Canada?",
            Column("birth_country", "TEXT", True, False), "=",
            hints=("Germany", "CAN"), client=client)
        self.assertEqual((result.value, result.status, result.source, result.jev_calls),
                         ("CAN", "resolved", "jev_grounded_observed_value", 2))

    def test_indecisive_compact_code_selection_gets_focused_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"value": {
                "choice": "observed_0", "confidence": .36,
                "probabilities": {"observed_0": .58, "none": .42},
            }}}, 120, 20, 60),
            CallResult({"answers": {"equivalence": {
                "choice": "observed", "confidence": .95,
                "probabilities": {"observed": .96, "none": .04},
            }}}, 80, 10, 40),
        ]
        result = resolve_predicate_value(
            "Canadian-born players", Column("birth_country", "TEXT", True, False), "=",
            hints=("CAN", "Netherlands"), client=client)
        self.assertEqual((result.value, result.status, result.jev_calls),
                         ("CAN", "resolved", 2))

    def test_decisive_review_choice_survives_separate_lower_confidence(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"value": {
                "choice": "none", "confidence": .64,
                "probabilities": {"observed_0": .24, "none": .76}}}}, 100, 10, 1),
            CallResult({"answers": {"equivalence": {
                "choice": "observed", "confidence": .71,
                "probabilities": {"observed": .86, "none": .14}}}}, 80, 8, 1),
        ]
        result = resolve_predicate_value(
            "players born in Canada", Column("birth_country", "TEXT", True, False), "=",
            hints=("CAN", "Germany"), client=client)
        self.assertEqual((result.value, result.status), ("CAN", "resolved"))

    def test_semantic_observed_value_can_be_grounded_without_literal_spelling(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"value": {
            "choice": "observed_0", "confidence": .94,
            "probabilities": {"observed_0": .94, "none": .06},
        }}}, 120, 20, 70)
        result = resolve_predicate_value(
            "Canadian-born players", Column("birth_country", "TEXT", True, False), "=",
            hints=("CAN",), client=client)
        self.assertEqual((result.value, result.status, result.source),
                         ("CAN", "resolved", "jev_grounded_observed_value"))

    def test_semantic_observed_value_can_fail_closed_to_none(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"value": {
            "choice": "none", "confidence": .92,
            "probabilities": {"observed_0": .08, "none": .92},
        }}}, 120, 20, 70)
        result = resolve_predicate_value(
            "something unrelated", Column("kind", "TEXT", True, False), "=",
            hints=("ABC",), client=client)
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.value)

    def test_null_needs_no_value(self):
        result = resolve_predicate_value("missing topic", Column("topic", "TEXT", True, False),
                                         "IS NULL")
        self.assertIsNone(result.value)
        self.assertEqual(result.jev_calls, 0)


if __name__ == "__main__":
    unittest.main()

class PredicateValueStrongObservedEvidenceTests(unittest.TestCase):
    def test_profiled_pattern_can_be_semantically_bound_for_like(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"value": {
            "choice": "pattern_6", "confidence": .88,
            "probabilities": {"pattern_6": .86, "request_0": .08},
        }}}, 100, 10, 1)
        result = resolve_predicate_value(
            "entities that are no longer active", Column("name", "TEXT", True, False),
            "SUFFIX", pattern_hints=("(inactive)",), client=client)
        self.assertEqual((result.value, result.status, result.source),
                         ("(inactive)", "resolved", "jev_grounded_observed_pattern"))

    def test_uncertain_pattern_choice_gets_focused_equivalence_review(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"value": {"choice": "pattern_4", "confidence": .35,
                "probabilities": {"pattern_4": .48, "request_2": .22}}}}, 100, 10, 1),
            CallResult({"answers": {"equivalent": {"noul": .91}}}, 40, 4, 1),
        ]
        result = resolve_predicate_value(
            "items no longer active", Column("name", "TEXT", True, False),
            "CONTAINS", pattern_hints=("(inactive)",), client=client)
        self.assertEqual((result.value, result.status, result.jev_calls),
                         ("(inactive)", "resolved", 2))
    def test_decisive_probability_can_ground_observed_category(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"value": {
            "choice": "observed_0", "confidence": .55,
            "probabilities": {"observed_0": .7, "observed_1": .11, "none": .19},
        }}}, 100, 10, 1)
        result = resolve_predicate_value(
            "public schools, but exclude charter schools",
            Column("type", "TEXT", True, False), "!=",
            hints=("Charter School", "Public School"), client=client)
        self.assertEqual((result.value, result.status), ("Charter School", "resolved"))
    def test_partial_category_token_requires_semantic_binding(self):
        client = Mock()
        def answer(state, questions):
            key = next(key for key, item in questions["value"]["criteria"].items()
                       if isinstance(item, dict) and item.get("value") == "Public School")
            return CallResult({"answers": {"value": {"choice": key,
                "confidence": .99, "probabilities": {key: .99}}}}, 20, 3, 1)
        client.call.side_effect = answer
        result = resolve_predicate_value(
            "Count schools in Worcester, excluding the public ones.",
            Column("type", "TEXT", True, False), "!=",
            hints=("Public School", "Charter School"), client=client)
        self.assertEqual((result.value, result.status, result.source),
                         ("Public School", "resolved", "jev_grounded_observed_value"))
        client.call.assert_called_once()

    def test_exact_observed_code_binds_without_semantic_alias_guessing(self):
        client = Mock()
        result = resolve_predicate_value(
            "Count players whose birth country isn't USA.",
            Column("birth_country", "TEXT", True, False), "!=",
            hints=("USA", "CAN", "D.R."), client=client)
        self.assertEqual((result.value, result.status, result.source),
                         ("USA", "resolved", "observed_request_evidence"))
        client.call.assert_not_called()
