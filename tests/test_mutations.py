import sqlite3
from intentsql.database import connect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intentsql import mutations, read_engine as engine
from intentsql.semantic_read import Condition, ConditionPlan
from intentsql import web
from fastapi import HTTPException


class MutationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "test.db"
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, score INTEGER)")
            conn.execute("INSERT INTO people VALUES (1, 'A', 10)")
        with connect(self.path) as conn:
            self.schema = engine.inspect_schema(conn)

    def tearDown(self):
        self.folder.cleanup()

    def test_preview_does_not_write_and_commit_uses_bound_values(self):
        program = mutations.MutationProgram("UPDATE", "people", {"name": "O'Reilly"}, "id", 1)
        sql, params = mutations.compile_mutation(program, self.schema)
        self.assertIn("?", sql)
        with connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            before = mutations.inspect_rows(conn, program)
            preview = mutations.preview_mutation(conn, program, sql, params, before)
        self.assertEqual(preview["after"][0]["name"], "O'Reilly")
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT name FROM people WHERE id=1").fetchone()[0], "A")
        plan = {"supported": True, "vet": {"passed": True}, "sql": sql, "params": params,
                "affected": 1, "fingerprint": mutations.fingerprint(self.path)}
        self.assertEqual(mutations.commit_mutation(self.path, plan), 1)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT name FROM people WHERE id=1").fetchone()[0], "O'Reilly")

    def test_unknown_columns_and_unbounded_writes_are_rejected(self):
        invalid = [
            mutations.MutationProgram("DELETE", "people"),
            mutations.MutationProgram("UPDATE", "people", {"missing": "x"}, "id", 1),
            mutations.MutationProgram("UPDATE", "people", {"id": 2}, "id", 1),
            mutations.MutationProgram("INSERT", "unknown", {"name": "A"}),
        ]
        for program in invalid:
            with self.subTest(program=program), self.assertRaises(ValueError):
                mutations.compile_mutation(program, self.schema)

    def test_stale_preview_cannot_commit(self):
        program = mutations.MutationProgram("DELETE", "people", {}, "id", 1)
        sql, params = mutations.compile_mutation(program, self.schema)
        plan = {"supported": True, "vet": {"passed": True}, "sql": sql, "params": params,
                "affected": 1, "fingerprint": mutations.fingerprint(self.path)}
        with connect(self.path) as conn:
            conn.execute("INSERT INTO people VALUES (2, 'B', 20)")
        with self.assertRaises(ValueError):
            mutations.commit_mutation(self.path, plan)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], 2)

    def test_insert_and_delete_preview_are_isolated_until_commit(self):
        for program, expected_after, expected_count in (
            (mutations.MutationProgram("INSERT", "people", {"name": "B", "score": 20}), "B", 2),
            (mutations.MutationProgram("DELETE", "people", {}, "id", 1), None, 0),
        ):
            with self.subTest(operation=program.operation):
                sql, params = mutations.compile_mutation(program, self.schema)
                with connect(self.path) as conn:
                    conn.row_factory = sqlite3.Row
                    before = mutations.inspect_rows(conn, program)
                    preview = mutations.preview_mutation(conn, program, sql, params, before)
                self.assertEqual(preview["affected"], 1)
                self.assertEqual(preview["after"][0]["name"] if preview["after"] else None,
                                 expected_after)
                with connect(self.path) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1)
                plan = {"supported": True, "vet": {"passed": True}, "sql": sql,
                        "params": params, "affected": 1,
                        "fingerprint": mutations.fingerprint(self.path)}
                self.assertEqual(mutations.commit_mutation(self.path, plan), 1)
                with connect(self.path) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0],
                                     expected_count)
                    conn.execute("DELETE FROM people")
                    conn.execute("INSERT INTO people VALUES (1, 'A', 10)")

    def test_first_row_delete_uses_stable_primary_key_and_preview(self):
        with connect(self.path) as conn:
            conn.execute("INSERT INTO people VALUES (3, 'C', 30)")
            conn.execute("INSERT INTO people VALUES (2, 'B', 20)")
        program = mutations.MutationProgram(
            "DELETE", "people", target_mode="first",
            stable_order_columns=("id",))
        sql, params = mutations.compile_mutation(program, self.schema)
        self.assertEqual(params, [])
        self.assertIn('ORDER BY "id" ASC LIMIT 1', sql)
        with connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            before = mutations.inspect_rows(conn, program)
            preview = mutations.preview_mutation(conn, program, sql, params, before)
        self.assertEqual([row["id"] for row in before], [1])
        self.assertEqual(preview["affected"], 1)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], 3)

    def test_vet_and_affected_row_guards_never_commit(self):
        program = mutations.MutationProgram("DELETE", "people", {}, "id", 1)
        sql, params = mutations.compile_mutation(program, self.schema)
        plan = {"supported": True, "vet": {"passed": False}, "sql": sql,
                "params": params, "affected": 1,
                "fingerprint": mutations.fingerprint(self.path)}
        with self.assertRaisesRegex(ValueError, "vetted"):
            mutations.commit_mutation(self.path, plan)
        plan["vet"]["passed"] = True
        plan["affected"] = 2
        with self.assertRaisesRegex(ValueError, "rolled back"):
            mutations.commit_mutation(self.path, plan)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1)

    def test_preview_rejects_more_than_row_cap(self):
        with connect(self.path) as conn:
            conn.executemany("INSERT INTO people (name, score) VALUES (?, 10)",
                             [(str(i),) for i in range(mutations.MAX_AFFECTED)])
        program = mutations.MutationProgram("DELETE", "people", {}, "score", 10)
        sql, params = mutations.compile_mutation(program, self.schema)
        with connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            before = mutations.inspect_rows(conn, program)
            self.assertEqual(len(before), mutations.MAX_AFFECTED + 1)
            with self.assertRaisesRegex(ValueError, "Unsafe affected-row count"):
                mutations.preview_mutation(conn, program, sql, params, before)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], 101)

    def test_web_requires_confirmation_and_consumes_token_before_undo(self):
        program = mutations.MutationProgram("UPDATE", "people", {"score": 20}, "id", 1)
        sql, params = mutations.compile_mutation(program, self.schema)
        plan = {"supported": True, "vet": {"passed": True}, "sql": sql,
                "params": params, "affected": 1,
                "fingerprint": mutations.fingerprint(self.path)}
        with patch.dict(web.PENDING, {"testtoken": {"database": "test.db", "plan": plan}}, clear=True), \
             patch.dict(web.UNDO, {}, clear=True), \
             patch.object(web, "database_path", return_value=self.path), \
             patch.object(web, "BACKUP_DIR", Path(self.folder.name)), \
             patch.object(web, "schema", return_value={}):
            with self.assertRaises(HTTPException):
                web.commit(web.CommitInput(token="testtoken", confirm=False))
            self.assertIn("testtoken", web.PENDING)
            result = web.commit(web.CommitInput(token="testtoken", confirm=True))
            self.assertEqual(result["affected"], 1)
            with self.assertRaises(HTTPException):
                web.commit(web.CommitInput(token="testtoken", confirm=True))
            with connect(self.path) as conn:
                self.assertEqual(conn.execute("SELECT score FROM people WHERE id=1").fetchone()[0], 20)
            self.assertTrue(web.undo(web.UndoInput(token="testtoken"))["restored"])
            with connect(self.path) as conn:
                self.assertEqual(conn.execute("SELECT score FROM people WHERE id=1").fetchone()[0], 10)

    def test_delete_can_use_unquoted_exact_existing_text(self):
        def fake_select(question, schema, program, stage, instruction, candidates):
            if stage == "choose operation":
                return "DELETE"
            if stage == "choose table":
                return "people"
            if stage == "choose target shape":
                return "predicate"
            raise AssertionError(stage)

        with patch.object(mutations, "select", side_effect=fake_select), \
             patch.object(mutations, "plan_conditions", return_value=ConditionPlan(
                 (Condition("name", "=", "A"),), "AND", ())), \
             patch.object(mutations, "vet_mutation", return_value={
                 "passed": True, "checks": {}, "verdict": "PASS", "raw": {}}):
            plan = mutations.plan_mutation(self.path,
                                           "Delete the person whose name is A.")
        self.assertTrue(plan["supported"])
        self.assertEqual(plan["sql"], 'DELETE FROM "people" WHERE "name" = ?')
        self.assertEqual(plan["params"], ["A"])
        self.assertEqual(plan["affected"], 1)

    def test_delete_canonicalizes_one_case_insensitive_existing_value(self):
        with connect(self.path) as conn:
            conn.execute("INSERT INTO people VALUES (2, 'Sales', 20)")

        def fake_select(question, schema, program, stage, instruction, candidates):
            if stage == "choose operation":
                return "DELETE"
            if stage == "choose table":
                return "people"
            if stage == "choose target shape":
                return "predicate"
            raise AssertionError(stage)

        with patch.object(mutations, "select", side_effect=fake_select), \
             patch.object(mutations, "plan_conditions", return_value=ConditionPlan(
                 (Condition("name", "=", "Sales"),), "AND", ())), \
             patch.object(mutations, "vet_mutation", return_value={
                 "passed": True, "checks": {}, "verdict": "PASS", "raw": {}}):
            plan = mutations.plan_mutation(
                self.path, "Delete every person in the sales category.")
        self.assertEqual(plan["params"], ["Sales"])
        self.assertEqual(plan["affected"], 1)
        self.assertEqual(plan["program"]["conditions"][0]["value"], "Sales")

    def test_case_insensitive_mutation_evidence_fails_closed_when_ambiguous(self):
        with connect(self.path) as conn:
            conn.execute("INSERT INTO people VALUES (2, 'Sales', 20)")
            conn.execute("INSERT INTO people VALUES (3, 'SALES', 30)")
        with connect(self.path) as conn:
            found, value = mutations._canonical_existing_value(
                conn, "people", self.schema.tables["people"].columns[1], "sales")
        self.assertFalse(found)
        self.assertIsNone(value)

    def test_mutation_without_explicit_literal_is_rejected(self):
        def fake_select(question, schema, program, stage, instruction, candidates):
            return {"choose operation": "DELETE", "choose table": "people",
                    "choose target shape": "predicate"}[stage]
        with patch.object(mutations, "select", side_effect=fake_select), \
             patch.object(mutations, "plan_conditions", return_value=ConditionPlan(
                 (), "AND", ())), \
             self.assertRaisesRegex(ValueError, "No source-row predicate"):
            mutations.plan_mutation(self.path, "Delete every person")

    def test_delete_first_row_plans_without_inventing_a_literal(self):
        def fake_select(question, schema, program, stage, instruction, candidates):
            return {"choose operation": "DELETE", "choose table": "people",
                    "choose target shape": "first"}[stage]
        with patch.object(mutations, "select", side_effect=fake_select), \
             patch.object(mutations, "vet_mutation", return_value={
                 "passed": True, "checks": {}, "verdict": "PASS", "raw": {}}):
            plan = mutations.plan_mutation(self.path, "Delete the first row in the people table.")
        self.assertTrue(plan["supported"])
        self.assertEqual(plan["params"], [])
        self.assertEqual(plan["before"][0]["id"], 1)
        self.assertIn('ORDER BY "id" ASC LIMIT 1', plan["sql"])


    def test_numeric_affinity_iso_date_assignment_uses_observed_format(self):
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, air_date NUMERIC, title TEXT)")
            conn.execute("INSERT INTO events (air_date, title) VALUES ('2026-01-02', 'Old')")
            schema = engine.inspect_schema(conn)
            air_date = next(column for column in schema.tables["events"].columns
                            if column.name == "air_date")
            evidence = mutations.literals_from_request(
                'Insert an event with air date "2098-05-10".')
            date_item = next(item for item in evidence
                             if str(item.value) == "2098-05-10")
            with patch.object(mutations.engine, "system_one", return_value={
                    "answers": {"a0": {"choice": date_item.evidence_id}},
                    "usage": {"input_tokens": 1, "output_tokens": 1}}):
                program = mutations.MutationProgram("INSERT", "events")
                mutations.bind_assignments(
                    'Insert an event with air date "2098-05-10".',
                    program, [air_date], evidence, conn)
            self.assertEqual(program.assignments["air_date"], "2098-05-10")

    def test_update_predicate_can_reuse_assignment_column_with_old_value(self):
        with connect(self.path) as conn:
            conn.execute("ALTER TABLE people ADD COLUMN city TEXT")
            conn.execute("UPDATE people SET city='Boston' WHERE id=1")
            columns = engine.inspect_schema(conn).tables["people"].columns
            grounded = mutations._mechanically_grounded_predicate_columns(
                conn, "people", columns,
                "Update every Boston person and set the city to     .")
        self.assertIn("city", grounded)


if __name__ == "__main__":
    unittest.main()
