"""Generic regressions for operand and measure mistakes in the Alpha check."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.semantic_read import _can_append_conjunctive_filter
from intentsql.skills.aggregate import resolve_aggregate
from intentsql.skills.facts import extract_facts
from intentsql.skills.fields import resolve_distinct_target
from intentsql.skills.predicate_value import resolve_predicate_value
from intentsql.skills.result_cardinality import resolve_result_cardinality
from intentsql.skills.schema import Column


class AlphaSafetyTests(unittest.TestCase):
    def test_uncertain_single_extremum_cannot_silently_preserve_ties(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"cardinality": {
            "choice": "top_one", "confidence": .4,
            "probabilities": {"top_one": .55, "all": .45}}}}, 90, 12, 1)
        result = resolve_result_cardinality("last release date", {"column": "released", "direction": "DESC"},
                                            client, tie_preserving_extremum=True)
        self.assertEqual(result.status, "ambiguous")

    def test_explicit_ties_remain_supported(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"cardinality": {
            "choice": "all", "confidence": .95}}}, 90, 12, 1)
        result = resolve_result_cardinality("all entities sharing the largest value; preserve ties",
                                            {"column": "metric", "direction": "DESC"},
                                            client, tie_preserving_extremum=True)
        self.assertEqual((result.mode, result.status), ("all", "resolved"))

    def test_conjunctive_repair_cannot_rewrite_cross_column_or(self):
        self.assertFalse(_can_append_conjunctive_filter("OR", 2))
        self.assertFalse(_can_append_conjunctive_filter("OR", 3))
        self.assertTrue(_can_append_conjunctive_filter("OR", 1))
        self.assertTrue(_can_append_conjunctive_filter("AND", 2))

    def test_formatted_quotes_preserve_trimmed_operand_and_raw_provenance(self):
        for quoted in ('"  Mixed Case  "', "`` Mixed Case ''", "' Mixed Case '"):
            fact, = extract_facts("exclude " + quoted)
            self.assertEqual(fact.value, "Mixed Case")
            self.assertEqual(fact.raw, quoted)
            self.assertEqual((fact.start, fact.end), (8, 8 + len(quoted)))

    def test_negated_literal_cannot_be_replaced_by_stored_case_variant(self):
        client = Mock()
        result = resolve_predicate_value(
            'exclude origin "Mixed Case"', Column("origin", "TEXT", True, False),
            "!=", hints=("mixed case",), client=client)
        self.assertEqual(result.value, "Mixed Case")
        self.assertEqual(result.source, "request_literal")
        client.call.assert_not_called()

    def test_conflicting_numeric_and_semantic_extreme_rejects(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "function": {"choice": "MAX"}, "target": {"choice": "c0"},
            "extremum": {"choice": "MIN"}}}, 90, 12, 1)
        result = resolve_aggregate("best placing", "results",
                                   (Column("position", "INT", True, False),), client)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.jev_calls, 1)

    def test_counting_records_does_not_become_count_distinct(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c0"}, "count_grain": {"choice": "records"}}}, 90, 12, 1)
        result = resolve_distinct_target("how many entries", "log",
                                        (Column("tag", "TEXT", True, False),), client)
        self.assertEqual((result.source, result.columns, result.status),
                         ("jev_row_count", (), "resolved"))

    def test_uncertain_counting_unit_rejects_even_for_one_column(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c0"}, "count_grain": {"choice": "ambiguous"}}}, 90, 12, 1)
        result = resolve_distinct_target("how many labels", "log",
                                        (Column("tag", "TEXT", True, False),), client)
        self.assertEqual(result.status, "ambiguous")
