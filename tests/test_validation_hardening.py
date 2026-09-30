"""Regression tests for failures found by the unified release validation run."""

import sqlite3
import unittest
from unittest.mock import Mock

from intentsql.decision_context import DecisionClient, ReadState
from intentsql.semantic_read import (
    Condition,
    _can_apply_row_limit,
    _can_probe_unique_source_row,
    _count_depends_on_unnamed_neighbor,
    _can_reanchor_scalar_aggregate,
    _is_scalar_aggregate_result,
    _should_retry_missing_join_filter,
    _drop_shadowed_duplicate_conditions,
    _keep_global_extremum_for_limit,
    _literal_facts_require_source_filter,
    _prefer_explicit_condition_order,
    plan_conditions,
)
from intentsql.skills.entity import Entity
from intentsql.skills.facts import extract_facts
from intentsql.skills.schema import Column


class ValidationHardeningTests(unittest.TestCase):
    def test_explicit_predicate_column_outranks_same_operand_lookalike(self):
        request = "Set flag to 1 for episodes with category Sci."
        conditions = [
            Condition("code", "PREFIX", "Sci"),
            Condition("category", "PREFIX", "Sci"),
        ]
        remaining = _drop_shadowed_duplicate_conditions(request, conditions)
        self.assertEqual(remaining, [Condition("category", "PREFIX", "Sci")])

    def test_explicit_predicate_order_is_schema_iteration_independent(self):
        request = "Show records with category Sci."
        ordered = _prefer_explicit_condition_order(request, [
            Condition("code", "PREFIX", "Sci"),
            Condition("category", "PREFIX", "Sci"),
        ])
        self.assertEqual([item.column for item in ordered], ["category", "code"])

    def test_scalar_aggregate_can_reanchor_only_when_original_source_adds_no_filter(self):
        self.assertTrue(_can_reanchor_scalar_aggregate("other", False, False, False))
        self.assertFalse(_can_reanchor_scalar_aggregate("other", False, False, True))
        self.assertFalse(_can_reanchor_scalar_aggregate("other", True, False, False))
        self.assertFalse(_can_reanchor_scalar_aggregate("fields", False, False, False))

    def test_top_n_does_not_keep_single_global_extremum_constraint(self):
        self.assertTrue(_keep_global_extremum_for_limit(True, None))
        self.assertTrue(_keep_global_extremum_for_limit(True, 1))
        self.assertFalse(_keep_global_extremum_for_limit(True, 10))
        self.assertFalse(_keep_global_extremum_for_limit(False, 1))


    def test_scalar_aggregate_shape_owns_order_and_limit_semantics(self):
        self.assertTrue(_is_scalar_aggregate_result("count", False))
        self.assertTrue(_is_scalar_aggregate_result("other", False))
        self.assertFalse(_is_scalar_aggregate_result("count", True))
        self.assertFalse(_is_scalar_aggregate_result("fields", False))
        self.assertFalse(_can_apply_row_limit("count", False))
        self.assertFalse(_can_apply_row_limit("other", False))
        self.assertTrue(_can_apply_row_limit("count", True))
        self.assertTrue(_can_apply_row_limit("fields", False))

    def test_missing_join_filter_retry_is_one_shot_and_only_when_empty(self):
        from intentsql.skills.schema import Relation

        edge = Relation("facts", "entity_id", "entities", "id")
        self.assertTrue(_should_retry_missing_join_filter(
            "missing_filter", edge, []))
        self.assertFalse(_should_retry_missing_join_filter(
            "complete", edge, []))
        self.assertFalse(_should_retry_missing_join_filter(
            "missing_filter", None, []))
        self.assertFalse(_should_retry_missing_join_filter(
            "missing_filter", edge, [Condition("score", "=", 100, "facts")]))

    def test_local_count_cannot_ignore_a_neighbor_only_measure(self):
        self.assertTrue(_count_depends_on_unnamed_neighbor("count", True, False, False))
        self.assertFalse(_count_depends_on_unnamed_neighbor("count", True, True, False))
        self.assertFalse(_count_depends_on_unnamed_neighbor("count", False, False, False))
        self.assertFalse(_count_depends_on_unnamed_neighbor("fields", True, False, False))
        self.assertFalse(_count_depends_on_unnamed_neighbor("count", True, False, True))

    def test_grouped_aggregate_is_not_a_unique_source_row_probe(self):
        self.assertFalse(_can_probe_unique_source_row("other", "year", "MAX", None))
        self.assertFalse(_can_probe_unique_source_row("rows", "year", None, None))
        self.assertFalse(_can_probe_unique_source_row("fields", None, "COUNT", None))
        self.assertFalse(_can_probe_unique_source_row("rows", None, None, "city"))
        self.assertFalse(_can_probe_unique_source_row("count", None, None, None))
        self.assertTrue(_can_probe_unique_source_row("rows", None, None, None))
        self.assertTrue(_can_probe_unique_source_row("fields", None, None, None))

    def test_explicit_interval_is_between_when_the_threshold_fact_is_excluded(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE performances (year INTEGER, HR INTEGER)")
        connection.execute("INSERT INTO performances VALUES (1990, 40), (2005, 70)")
        request = ("From 1990 through 2005, show the years whose maximum "
                   "home-run total was at least 60.")
        facts = extract_facts(request)
        threshold = next(fact for fact in facts if fact.value == 60)
        transport = Mock()
        transport.call.side_effect = AssertionError("interval ownership left the planner")
        client = DecisionClient(transport, ReadState(source="performances"))
        plan = plan_conditions(
            connection, request,
            Entity("performances", "resolved", "test"),
            (Column("year", "INTEGER", True, False),
             Column("HR", "INTEGER", True, False)),
            client, facts, lambda *_args: None, lambda _table, _column: None,
            needs_filter=True,
            established_columns=("year",),
            excluded_columns=("HR",),
            excluded_fact_ids=(threshold.fact_id,))
        self.assertEqual(plan.conditions, (Condition("year", "BETWEEN", (1990, 2005)),))
        transport.call.assert_not_called()

    def test_scalar_aggregate_literal_cannot_disappear_as_a_fake_row_limit(self):
        facts = extract_facts(
            "What was the average salary in 2001, rounded to two decimal places?")
        self.assertTrue(_literal_facts_require_source_filter(
            "What was the average salary in 2001, rounded to two decimal places?",
            facts, output_kind="other", grouping=False,
            has_group_threshold=False, round_places=2))

    def test_grouped_explicit_interval_survives_noisy_quantity_route(self):
        request = (
            "From 1990 through 2005, list every year by its maximum "
            "home-run total, greatest to smallest.")
        self.assertTrue(_literal_facts_require_source_filter(
            request, extract_facts(request), output_kind="other", grouping=True,
            has_group_threshold=False))

    def test_grouped_top_n_literal_is_not_automatically_a_source_filter(self):
        request = "Show the five years with the highest maximum home-run total."
        self.assertFalse(_literal_facts_require_source_filter(
            request, extract_facts(request), output_kind="other", grouping=True,
            has_group_threshold=False))

    def test_text_column_does_not_consume_an_explicit_numeric_interval(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE notes (label TEXT)")
        connection.execute("INSERT INTO notes VALUES ('alpha')")
        request = "From 1990 through 2005, show the matching notes."
        questions = []

        def stop(state, asked):
            questions.append(asked)
            raise AssertionError("operator decision")

        transport = Mock()
        transport.call.side_effect = stop
        client = DecisionClient(transport, ReadState(source="notes"))
        with self.assertRaises(AssertionError):
            plan_conditions(
                connection, request,
                Entity("notes", "resolved", "test"),
                (Column("label", "TEXT", True, False),),
                client, extract_facts(request), lambda *_args: None,
                lambda _table, _column: None,
                needs_filter=True, established_columns=("label",))
        self.assertIn("operator", questions[0])


if __name__ == "__main__":
    unittest.main()
