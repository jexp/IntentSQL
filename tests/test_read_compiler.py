"""Execution tests for compiler features that must not rely on Jev's wording."""

from intentsql.database import connect
import unittest
from collections import Counter
from pathlib import Path

from intentsql import read_engine as engine


DATA = Path(__file__).resolve().parents[1] / "data"


class ReadCompilerTests(unittest.TestCase):
    def test_ordinal_values_are_generated_from_schema_data_without_invented_ids(self):
        engine.reset_run_state()
        with connect(DATA / "cyberchase.db") as conn:
            schema = engine.inspect_schema(conn)
            engine.profile_categories(conn, schema)
            columns = engine.available_filter_columns(
                "Show titles from the original season", schema,
                engine.QueryProgram("episodes"))
            self.assertIn("episodes.season", [column.ref for column in columns])
            self.assertEqual(engine.RUN_ORDINAL_VALUES["episodes.season"][0], 1)
        engine.reset_run_state()
        with connect(DATA / "moneyball.db") as conn:
            schema = engine.inspect_schema(conn)
            engine.profile_categories(conn, schema)
            columns = engine.available_filter_columns(
                "Cal Ripken's salary history, newest year first", schema,
                engine.QueryProgram("salaries"))
            self.assertNotIn("salaries.player_id", [column.ref for column in columns])
            self.assertNotIn("salaries.year", [column.ref for column in columns])
        engine.reset_run_state()

    def test_grouped_condition_and_secondary_sort(self):
        program = engine.QueryProgram(
            "schools",
            outputs=[
                engine.SelectExpr("column", "schools", "city", alias="schools_city"),
                engine.SelectExpr("aggregate", func="COUNT_ROWS", alias="row_count"),
            ],
            filters=[engine.FilterSpec("schools", "type", "=", "Public School")],
            groups=[engine.GroupSpec("schools", "city")],
            having=[engine.HavingSpec("row_count", "<=", 3)],
            order_by=[engine.OrderSpec("row_count", "DESC"),
                      engine.OrderSpec("schools_city", "ASC")],
        )
        sql, params = engine.compile_sql(program)
        with connect(DATA / "dese.db") as conn:
            actual = conn.execute(sql, params).fetchall()
            expected = conn.execute("""
                SELECT city, COUNT(*) FROM schools WHERE type='Public School'
                GROUP BY city HAVING COUNT(*) <= 3
                ORDER BY COUNT(*) DESC, city ASC
            """).fetchall()
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 201)

    def test_extremum_metric_need_not_be_returned(self):
        program = engine.QueryProgram(
            "players",
            joins=[engine.JoinSpec("players", "id", "salaries", "player_id")],
            outputs=[engine.SelectExpr("column", "players", "first_name"),
                     engine.SelectExpr("column", "players", "last_name")],
            extremum={"key": "__extremum_metric", "mode": "max",
                      "source_table": "salaries", "source_column": "salary"},
        )
        sql, params = engine.compile_sql(program)
        self.assertTrue(engine.program_structurally_ready(program))
        with connect(DATA / "moneyball.db") as conn:
            actual = conn.execute(sql, params).fetchall()
            expected = conn.execute("""
                SELECT players.first_name, players.last_name FROM players
                JOIN salaries ON players.id=salaries.player_id
                WHERE salaries.salary=(SELECT MAX(salary) FROM salaries)
            """).fetchall()
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 1)

    def test_truncated_read_reports_exact_total_rows(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE numbers (n INTEGER)")
            conn.executemany("INSERT INTO numbers VALUES (?)", [(i,) for i in range(5)])
            previous = engine.MAX_RESULT_ROWS
            try:
                engine.MAX_RESULT_ROWS = 2
                columns, rows, truncated, total = engine.execute_query(
                    conn, "SELECT n FROM numbers ORDER BY n", [])
            finally:
                engine.MAX_RESULT_ROWS = previous
        self.assertEqual(columns, ["n"])
        self.assertEqual(rows, [(0,), (1,)])
        self.assertTrue(truncated)
        self.assertEqual(total, 5)

    def test_join_can_match_player_and_year_without_row_explosion(self):
        program = engine.QueryProgram(
            "players",
            joins=[
                engine.JoinSpec("players", "id", "salaries", "player_id"),
                engine.JoinSpec("players", "id", "performances", "player_id",
                                (("salaries", "year", "performances", "year"),)),
            ],
            outputs=[
                engine.SelectExpr("column", "players", "first_name"),
                engine.SelectExpr("column", "players", "last_name"),
                engine.SelectExpr("column", "salaries", "salary"),
                engine.SelectExpr("column", "performances", "HR"),
                engine.SelectExpr("column", "salaries", "year"),
            ],
        )
        sql, params = engine.compile_sql(program)
        with connect(DATA / "moneyball.db") as conn:
            actual = conn.execute(sql, params).fetchall()
            expected = conn.execute("""
                SELECT players.first_name, players.last_name, salaries.salary,
                       performances.HR, salaries.year
                FROM players JOIN salaries ON players.id=salaries.player_id
                JOIN performances ON players.id=performances.player_id
                  AND salaries.year=performances.year
            """).fetchall()
        self.assertEqual(Counter(actual), Counter(expected))
        self.assertEqual(len(actual), 14915)


if __name__ == "__main__":
    unittest.main()
