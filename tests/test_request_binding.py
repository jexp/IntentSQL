"""Regression coverage for exact request operands and result shape."""

import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.semantic_read import _row_equivalent_value_column
from intentsql.mutations import literals_from_request
from intentsql.skills.facts import extract_facts
from intentsql.skills.predicate_value import (has_request_evidence,
                                               resolve_predicate_value,
                                               value_candidates)
from intentsql.skills.schema import Column


class RequestBindingTests(unittest.TestCase):
    def test_row_equivalent_exact_columns_choose_stable_schema_column(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE places (name TEXT, local_name TEXT)")
        connection.executemany("INSERT INTO places VALUES (?, ?)", [
            ("Anguilla", "Anguilla"), ("Other", "Elsewhere")])
        chosen = _row_equivalent_value_column(
            connection, "places", ("name", "local_name"),
            {"name": ("Anguilla",), "local_name": ("Anguilla",)})
        self.assertEqual(chosen, "name")

    def test_worded_and_decimal_operands_keep_stable_spans(self):
        facts = extract_facts("from twelve million through fifteen million and price 44.50")
        self.assertEqual([(fact.kind, fact.value) for fact in facts],
                         [("worded_number", 12000000), ("worded_number", 15000000),
                          ("number", 44.5)])
        self.assertEqual(len({fact.fact_id for fact in facts}), 3)

    def test_digit_scaled_operands_are_single_exact_facts(self):
        facts = extract_facts(
            "Give me salaries no lower than 12 million and no higher than 15 million")
        self.assertEqual([(fact.kind, fact.value) for fact in facts],
                         [("worded_number", 12000000),
                          ("worded_number", 15000000)])
        self.assertEqual([fact.raw for fact in facts], ["12 million", "15 million"])

    def test_year_on_iso_date_column_binds_exact_year_not_request_words(self):
        request = "How many purchases were placed in 2026?"
        facts = extract_facts(request)
        column = Column("order_date", "TEXT", True, False)
        candidates = value_candidates(request, column, ("2026-01-03",), facts, "=",
                                      date_semantics=True)
        self.assertEqual([item["value"] for item in candidates.values()], [2026])
        result = resolve_predicate_value(request, column, "=", ("2026-01-03",),
                                         facts=facts, date_semantics=True)
        self.assertEqual((result.value, result.jev_calls), (2026, 0))
        self.assertIsNotNone(result.fact_id)


    def test_month_year_is_one_exact_calendar_fact(self):
        facts = extract_facts("How many completed orders were placed during February 2026?")
        month_facts = [fact for fact in facts if fact.kind == "month_year"]
        self.assertEqual([(fact.value, fact.raw) for fact in month_facts],
                         [("2026-02", "February 2026")])
        self.assertFalse(any(fact.kind == "integer" and fact.value == 2026 for fact in facts))

    def test_calendar_range_shares_trailing_year_without_jev(self):
        facts = extract_facts("Show records from January through September 2023")
        self.assertEqual([(fact.kind, fact.value) for fact in facts], [
            ("month_year", "2023-01"), ("month_year", "2023-09")])
        self.assertEqual(len({fact.fact_id for fact in facts}), 2)

    def test_pattern_operands_prefer_request_text_over_samples(self):
        column = Column("title", "TEXT", True, False)
        candidates = value_candidates("titles containing the word Hacker", column,
                                      ("Hacker Hugs a Tree",), (), "CONTAINS")
        self.assertIn("Hacker", {item["value"] for item in candidates.values()})
        self.assertNotIn("Hacker Hugs a Tree", {item["value"] for item in candidates.values()})

    def test_pattern_operands_preserve_single_character_request_span(self):
        column = Column("Name", "TEXT", True, False)
        candidates = value_candidates("rows where Name starts with A", column,
                                      ("Antal Brown",), (), "PREFIX")
        self.assertIn("A", {item["value"] for item in candidates.values()})
        self.assertNotIn("Antal Brown", {item["value"] for item in candidates.values()})

    def test_consumed_numeric_fact_cannot_bind_binary_column(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"value": {
            "choice": "observed_1", "probabilities": {"observed_1": .9}}}},
            100, 20, 100)
        facts = extract_facts("remote employees younger than 35")
        age_fact = next(fact for fact in facts if fact.value == 35)
        result = resolve_predicate_value("remote employees younger than 35",
            Column("remote", "INTEGER", True, False), "=", ("0", "1"),
            client, facts, {age_fact.fact_id})
        self.assertEqual(result.value, 1)

    def test_unmatched_sample_is_not_request_evidence(self):
        self.assertFalse(has_request_evidence("For Boston", "Cambridge Rindge and Latin",
                                              "jev_choice"))
        self.assertTrue(has_request_evidence("For Boston", "Boston", "jev_choice"))

    def test_mutation_evidence_contains_unquoted_identifiers_and_decimal(self):
        evidence = literals_from_request(
            "Add an inventory item with SKU NEW001, name New Test Item, stock 12, and price 44.50.")
        values = {item.value for item in evidence}
        self.assertIn("NEW001", values)
        self.assertIn("New Test Item", values)
        self.assertIn(44.5, values)
        self.assertEqual(len(values), len(evidence))

    def test_regression_catalog_keeps_all_stress_prompts(self):
        fixture = Path(__file__).parents[1] / "tools/fixtures/binding_regressions.json"
        cases = json.loads(fixture.read_text())["cases"]
        self.assertEqual(len(cases), 10)
        self.assertTrue(all(case.get("paraphrases") for case in cases))


if __name__ == "__main__":
    unittest.main()
