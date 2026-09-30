"""The new graph compiles only inspected identifiers and bound values."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from intentsql.semantic_read import Condition, SelectQuery, compile_select, run_read
from intentsql.skills.intent import Intent
from intentsql.skills.schema import Relation
from intentsql.skills.join_fields import QualifiedField


class CompileSelectTests(unittest.TestCase):
    def test_per_group_extremum_can_return_source_rows(self):
        query = SelectQuery("events", "rows", partition_column="category",
                            partition_extremum_column="occurred",
                            partition_extremum_function="MIN",
                            order_column="category", order_direction="ASC")
        sql, params = compile_select(query, ("id", "category", "occurred"))
        self.assertIn('SELECT * FROM "events"', sql)
        self.assertIn('MIN("grouped"."occurred")', sql)
        self.assertIn('ORDER BY "category" ASC', sql)
        self.assertEqual(params, [])

    def test_simple_rows(self):
        sql, params = compile_select(SelectQuery("items", "rows", limit=5), ("id", "name"))
        self.assertEqual((sql, params), ('SELECT * FROM "items" LIMIT ?', [5]))

    def test_bound_filter_order_and_distinct(self):
        query = SelectQuery("items", "fields", ("name",),
                            (Condition("name", "CONTAINS", "O'Reilly%"),),
                            "AND", "name", "DESC", 10, True)
        sql, params = compile_select(query, ("id", "name"))
        self.assertEqual(sql, '''SELECT DISTINCT "name" FROM "items" WHERE "name" LIKE ? ESCAPE '\\' ORDER BY "name" DESC LIMIT ?''')
        self.assertEqual(params, ["%O'Reilly\\%%", 10])

    def test_uninspected_column_rejected(self):
        with self.assertRaises(ValueError):
            compile_select(SelectQuery("items", "fields", ("secret",)), ("id",))

    def test_null_predicates_need_no_bound_operand(self):
        query = SelectQuery("tickets", "count",
                            conditions=(Condition("satisfaction", "IS NULL", None),))
        sql, params = compile_select(query, ("id", "satisfaction"))
        self.assertEqual(sql, 'SELECT COUNT(*) AS row_count FROM "tickets" WHERE "satisfaction" IS NULL')
        self.assertEqual(params, [])

    def test_secondary_ordering(self):
        query = SelectQuery("employees", "fields", ("name", "city"),
                            order_column="city", order_direction="ASC",
                            secondary_order_column="name", secondary_order_direction="ASC")
        sql, params = compile_select(query, ("name", "city"))
        self.assertEqual(sql, 'SELECT "name", "city" FROM "employees" ORDER BY "city" ASC, "name" ASC')
        self.assertEqual(params, [])

    def test_window_semantics_are_rejected_before_database_execution(self):
        intent = Intent(
            output="fields", filters=False, ordering=True, quantity="numeric",
            distinct=False, grouping=True, relationship=False, having=False,
            expression=False, windowing=True, scores={"filters": 0.0, "ordering": 1.0,
                "distinct": 0.0, "grouping": 1.0, "relationship": 0.0,
                "having": 0.0, "expression": 0.0, "windowing": 1.0},
            confidence={}, probabilities={}, input_tokens=10, output_tokens=5,
            elapsed_ms=1, raw_answers={})
        unsupported = Mock(status="unsupported", group_column=None,
                           extremum_column=None, function=None,
                           jev_calls=0, input_tokens=0, output_tokens=0, trace=())
        with patch("intentsql.semantic_read.route_intent", return_value=intent), \
             patch("intentsql.semantic_read.resolve_per_group_extremum", return_value=unsupported):
            with self.assertRaisesRegex(ValueError, "window/ranking"):
                run_read(Path(__file__).resolve().parents[1] / "data/cyberchase.db",
                         "Rank employees by salary within each department", Mock())

    def test_grouped_count_does_not_enter_row_window_branch(self):
        intent = Intent(
            output="count", filters=False, ordering=True, quantity="numeric",
            distinct=False, grouping=True, relationship=False, having=False,
            expression=False, windowing=True, scores={"filters": 0.0, "ordering": 1.0,
                "distinct": 0.0, "grouping": 1.0, "relationship": 0.0,
                "having": 0.0, "expression": 0.0, "windowing": 1.0},
            confidence={}, probabilities={}, input_tokens=10, output_tokens=5,
            elapsed_ms=1, raw_answers={})
        with patch("intentsql.semantic_read.route_intent", return_value=intent), \
             patch("intentsql.semantic_read.resolve_per_group_extremum") as extremum:
            with self.assertRaises(Exception):
                # Later mocked semantic calls are intentionally absent; this
                # test only proves the row-window resolver is never entered.
                run_read(Path(__file__).resolve().parents[1] / "data/cyberchase.db",
                         "count rows in each group", Mock())
            extremum.assert_not_called()

    def test_per_group_extremum_compiles_as_correlated_inspected_query(self):
        query = SelectQuery(
            "episodes", "fields", ("season", "title"),
            partition_column="season",
            partition_extremum_column="episode_in_season",
            partition_extremum_function="MIN",
        )
        sql, params = compile_select(
            query, ("id", "season", "episode_in_season", "title"))
        self.assertEqual(params, [])
        self.assertEqual(sql, (
            'SELECT "season", "title" FROM "episodes" '
            'WHERE "episodes"."episode_in_season" = '
            '(SELECT MIN("grouped"."episode_in_season") '
            'FROM "episodes" AS "grouped" '
            'WHERE "grouped"."season" = "episodes"."season")'))

    def test_direct_join_can_return_base_rows_with_qualified_predicates(self):
        relation = Relation("facts", "entity_id", "entities", "id")
        query = SelectQuery(
            "facts", "rows",
            conditions=(Condition("year", "BETWEEN", (2000, 2005), "facts"),
                        Condition("region", "!=", "X", "entities")),
            relation=relation)
        sql, params = compile_select(
            query, ("id", "entity_id", "year"),
            {"entities": ("id", "region")})
        self.assertTrue(sql.startswith('SELECT "facts".* FROM "facts" JOIN "entities"'))
        self.assertIn('"facts"."year" BETWEEN ? AND ?', sql)
        self.assertIn('"entities"."region" != ?', sql)
        self.assertEqual(params, [2000, 2005, "X"])

    def test_per_group_extremum_rejects_uninspected_or_incomplete_shape(self):
        with self.assertRaises(ValueError):
            compile_select(SelectQuery(
                "episodes", "fields", ("title",), partition_column="season",
                partition_extremum_column="missing", partition_extremum_function="MIN"),
                ("season", "title"))
        with self.assertRaises(ValueError):
            compile_select(SelectQuery(
                "episodes", "fields", ("title",), partition_column="season"),
                ("season", "title"))

    def test_count(self):
        sql, params = compile_select(SelectQuery("items", "count",
                            conditions=(Condition("city", "=", "Morocco"),)), ("id", "city"))
        self.assertEqual(sql, 'SELECT COUNT(*) AS row_count FROM "items" WHERE "city" = ?')
        self.assertEqual(params, ["Morocco"])

    def test_count_distinct(self):
        sql, params = compile_select(SelectQuery("episodes", "count", ("title",), distinct=True),
                                     ("id", "title"))
        self.assertEqual(sql, 'SELECT COUNT(DISTINCT "title") AS row_count FROM "episodes"')
        self.assertEqual(params, [])

    def test_grouped_average_can_have_separate_row_count_threshold(self):
        query = SelectQuery(
            "records", "aggregate", aggregate_function="AVG",
            aggregate_column="height", group_column="country",
            having_operator=">=", having_value=20,
            having_aggregate_function="COUNT")
        sql, params = compile_select(query, ("country", "height"))
        self.assertIn('AVG("height") AS "avg_height"', sql)
        self.assertIn('GROUP BY "country" HAVING COUNT(*) >= ?', sql)
        self.assertEqual(params, [20])

    def test_inclusive_range_is_parameterized(self):
        query = SelectQuery("events", "rows",
                            conditions=(Condition("occurred", "BETWEEN",
                                                  ("2018-01-01", "2023-12-31")),))
        sql, params = compile_select(query, ("occurred",))
        self.assertEqual(sql, 'SELECT * FROM "events" WHERE "occurred" BETWEEN ? AND ?')
        self.assertEqual(params, ["2018-01-01", "2023-12-31"])

    def test_two_conditions_compile_with_bound_values(self):
        query = SelectQuery("events", "fields", ("title",),
                            (Condition("season", "=", 6),
                             Condition("occurred", "BETWEEN", ("2007-01-01", "2007-12-31"))))
        sql, params = compile_select(query, ("title", "season", "occurred"))
        self.assertEqual(sql, 'SELECT "title" FROM "events" WHERE ("season" = ?) AND ("occurred" BETWEEN ? AND ?)')
        self.assertEqual(params, [6, "2007-01-01", "2007-12-31"])

    def test_or_conditions_are_parenthesized(self):
        query = SelectQuery("events", "rows", conditions=(
            Condition("season", "=", 2), Condition("topic", "CONTAINS", "Fractions")),
            connector="OR")
        sql, params = compile_select(query, ("season", "topic"))
        self.assertEqual(sql, '''SELECT * FROM "events" WHERE ("season" = ?) OR ("topic" LIKE ? ESCAPE '\\')''')
        self.assertEqual(params, [2, "%Fractions%"])

    def test_scalar_aggregate_uses_inspected_column(self):
        query = SelectQuery("expenditures", "aggregate", aggregate_function="AVG",
                            aggregate_column="per_pupil_expenditure")
        sql, params = compile_select(query, ("id", "per_pupil_expenditure"))
        self.assertEqual(sql, 'SELECT AVG("per_pupil_expenditure") AS "avg_per_pupil_expenditure" FROM "expenditures"')
        self.assertEqual(params, [])

    def test_group_count(self):
        query = SelectQuery("episodes", "count", group_column="season")
        sql, params = compile_select(query, ("id", "season"))
        self.assertEqual(sql, 'SELECT "season", COUNT(*) AS row_count FROM "episodes" GROUP BY "season"')
        self.assertEqual(params, [])

    def test_grouped_average_with_filter(self):
        query = SelectQuery("salaries", "aggregate",
            conditions=(Condition("year", ">=", 2000),),
            aggregate_function="AVG", aggregate_column="salary", group_column="year")
        sql, params = compile_select(query, ("year", "salary"))
        self.assertEqual(sql, 'SELECT "year", AVG("salary") AS "avg_salary" FROM "salaries" WHERE "year" >= ? GROUP BY "year"')
        self.assertEqual(params, [2000])

    def test_grouped_rounded_average_binds_precision_before_filter(self):
        query = SelectQuery("salaries", "aggregate",
            conditions=(Condition("year", ">=", 2000),),
            aggregate_function="AVG", aggregate_column="salary", group_column="year",
            group_order_target="key", group_order_direction="DESC", round_places=2)
        sql, params = compile_select(query, ("year", "salary"))
        self.assertEqual(sql, 'SELECT "year", ROUND(AVG("salary"), ?) AS "avg_salary" FROM "salaries" WHERE "year" >= ? GROUP BY "year" ORDER BY "year" DESC')
        self.assertEqual(params, [2, 2000])

    def test_rounding_rejects_wrong_shape_or_precision(self):
        with self.assertRaises(ValueError):
            compile_select(SelectQuery("salaries", "count", round_places=2), ("salary",))
        with self.assertRaises(ValueError):
            compile_select(SelectQuery("salaries", "aggregate", aggregate_function="AVG",
                                       aggregate_column="salary", round_places=7), ("salary",))

    def test_grouped_metric_order_and_key_ties(self):
        query = SelectQuery("schools", "count", group_column="city",
                            group_order_target="measure", group_order_direction="DESC",
                            group_tie_direction="ASC", limit=10)
        sql, params = compile_select(query, ("city", "type"))
        self.assertEqual(sql, 'SELECT "city", COUNT(*) AS row_count FROM "schools" GROUP BY "city" ORDER BY "row_count" DESC, "city" ASC LIMIT ?')
        self.assertEqual(params, [10])

    def test_having_uses_computed_count_and_bound_threshold(self):
        query = SelectQuery("schools", "count", group_column="city",
                            having_operator="<=", having_value=3)
        sql, params = compile_select(query, ("city",))
        self.assertEqual(sql, 'SELECT "city", COUNT(*) AS row_count FROM "schools" GROUP BY "city" HAVING COUNT(*) <= ?')
        self.assertEqual(params, [3])

    def test_one_declared_join_uses_qualified_inspected_fields(self):
        edge = Relation("graduation_rates", "school_id", "schools", "id")
        query = SelectQuery("graduation_rates", "fields",
            conditions=(Condition("graduated", "=", 100, "graduation_rates"),),
            relation=edge, qualified_columns=(QualifiedField("schools", "name"),))
        sql, params = compile_select(query, ("school_id", "graduated"),
                                     {"schools": ("id", "name")})
        self.assertEqual(sql, 'SELECT "schools"."name" AS "schools_name" FROM "graduation_rates" JOIN "schools" ON "graduation_rates"."school_id" = "schools"."id" WHERE "graduation_rates"."graduated" = ?')
        self.assertEqual(params, [100])

    def test_unknown_join_field_rejected(self):
        edge = Relation("children", "parent_id", "parents", "id")
        query = SelectQuery("children", "fields", relation=edge,
                            qualified_columns=(QualifiedField("parents", "secret"),))
        with self.assertRaises(ValueError):
            compile_select(query, ("parent_id",), {"parents": ("id",)})

    def test_joined_order_uses_inspected_qualified_column(self):
        edge = Relation("expenditures", "district_id", "districts", "id")
        query = SelectQuery("districts", "fields", relation=edge,
            qualified_columns=(QualifiedField("districts", "name"),
                               QualifiedField("expenditures", "pupils")),
            order_column="pupils", order_direction="DESC", order_table="expenditures")
        sql, params = compile_select(query, ("id", "name"),
                                     {"expenditures": ("district_id", "pupils")})
        self.assertIn('ORDER BY "expenditures"."pupils" DESC', sql)
        self.assertEqual(params, [])
        with self.assertRaises(ValueError):
            compile_select(SelectQuery("districts", "fields", relation=edge,
                qualified_columns=(QualifiedField("districts", "name"),),
                order_column="secret", order_direction="ASC", order_table="expenditures"),
                ("id", "name"), {"expenditures": ("district_id", "pupils")})

    def test_joined_global_extremum_preserves_ties(self):
        edge = Relation("expenditures", "district_id", "districts", "id")
        query = SelectQuery(
            "expenditures", "fields", relation=edge,
            qualified_columns=(QualifiedField("districts", "name"),),
            global_extremum_table="expenditures", global_extremum_column="pupils",
            global_extremum_function="MIN")
        sql, params = compile_select(query, ("district_id", "pupils"),
                                     {"districts": ("id", "name")})
        self.assertIn(
            '"expenditures"."pupils" = (SELECT MIN("pupils") FROM "expenditures")',
            sql)
        self.assertNotIn("LIMIT", sql)
        self.assertEqual(params, [])


if __name__ == "__main__":
    unittest.main()
