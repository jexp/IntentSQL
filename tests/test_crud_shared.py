"""The read and write compilers must preserve the same row-condition meaning."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intentsql import mutations, read_engine
from intentsql import web
from intentsql.database import connect
from intentsql.semantic_read import Condition, SelectQuery, compile_conditions, compile_select
from intentsql.skills.predicate_value import value_candidates
from intentsql.skills.schema import Column
from fastapi import HTTPException


class SharedCrudConditionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "conditions.sqlite"
        with connect(self.path) as conn:
            conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, label TEXT, "
                         "amount INTEGER, date TEXT, category TEXT, note TEXT)")
            conn.executemany("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)", [
                (1, "Alpha", 5, "2020-01-01", "Drama", None),
                (2, "Star One", 8, "2021-03-01", "Comedy", "first"),
                (3, "Star Two", 9, "2023-04-01", "Drama", "second"),
                (4, "Beta One", 7, "2023-08-01", "Comedy", None),
            ])
            self.schema = read_engine.inspect_schema(conn)

    def tearDown(self):
        self.folder.cleanup()

    def test_typed_condition_matrix_has_identical_read_update_delete_targets(self):
        cases = [
            ((Condition("label", "=", "Alpha"),), "AND"),
            ((Condition("id", "=", 3),), "AND"),
            ((Condition("amount", ">", 7),), "AND"),
            ((Condition("amount", "BETWEEN", (5, 8)),), "AND"),
            ((Condition("date", "=", "2023-04-01"),), "AND"),
            ((Condition("date", "BETWEEN", ("2023-01-01", "2023-12-31")),), "AND"),
            ((Condition("date", "BETWEEN", ("2023-04-01", "2023-08-01")),), "AND"),
            ((Condition("category", "IN", ("Drama", "Comedy")),), "AND"),
            ((Condition("category", "!=", "Drama"),), "AND"),
            ((Condition("note", "IS NULL", None),), "AND"),
            ((Condition("note", "IS NOT NULL", None),), "AND"),
            ((Condition("label", "CONTAINS", "Star"),), "AND"),
            ((Condition("label", "PREFIX", "Star"),), "AND"),
            ((Condition("label", "SUFFIX", "One"),), "AND"),
            ((Condition("category", "=", "Drama"), Condition("amount", ">", 7)), "AND"),
            ((Condition("category", "=", "Drama"), Condition("category", "=", "Comedy")), "OR"),
        ]
        columns = tuple(column.name for column in self.schema.tables["entries"].columns)
        for conditions, connector in cases:
            with self.subTest(conditions=conditions, connector=connector):
                select_sql, select_params = compile_select(
                    SelectQuery("entries", "rows", conditions=conditions, connector=connector),
                    columns)
                with connect(self.path) as conn:
                    expected = [row[0] for row in conn.execute(
                        "SELECT id FROM entries WHERE " +
                        compile_conditions(conditions, connector, columns)[0],
                        select_params)]
                    self.assertEqual([row[0] for row in conn.execute(select_sql, select_params)],
                                     expected)
                    conn.row_factory = sqlite3.Row
                    for operation, assignments in (("UPDATE", {"amount": 11}), ("DELETE", {})):
                        program = mutations.MutationProgram(
                            operation, "entries", assignments, target_mode="predicate",
                            conditions=conditions, connector=connector)
                        sql, params = mutations.compile_mutation(program, self.schema)
                        self.assertIn(" WHERE ", sql)
                        self.assertEqual([row["id"] for row in mutations.inspect_rows(conn, program)],
                                         expected)
                        preview = mutations.preview_mutation(
                            conn, program, sql, params, mutations.inspect_rows(conn, program))
                        self.assertEqual(preview["affected"], len(expected))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 4)

    def test_uninspected_or_incomplete_predicates_fail_closed(self):
        for conditions in ((), (Condition("missing", "=", 1),),
                           (Condition("id", "=", None),),
                           (Condition("id", "BETWEEN", (1,)),),
                           (Condition("label", "REGEXP", "A"),)):
            with self.subTest(conditions=conditions), self.assertRaises(ValueError):
                mutations.compile_mutation(mutations.MutationProgram(
                    "DELETE", "entries", target_mode="predicate", conditions=conditions),
                    self.schema)

    def test_explicit_set_column_is_grounded_without_jev(self):
        program = mutations.MutationProgram("UPDATE", "entries")
        columns = self.schema.tables["entries"].columns
        with patch.object(read_engine, "system_one", return_value={
            "answers": {"c0": {"noul": .04}, "c1": {"noul": .98}}}) as call:
            selected = mutations.assignment_columns(
                "Set amount to 11 for entries whose label is Alpha.", program,
                [column for column in columns if column.name in {"label", "amount"}])
        self.assertEqual(selected, ("amount",))
        call.assert_not_called()

    def test_ambiguous_assignment_roles_are_batched(self):
        program = mutations.MutationProgram("UPDATE", "entries")
        columns = [column for column in self.schema.tables["entries"].columns
                   if column.name in {"label", "amount"}]
        with patch.object(read_engine, "system_one", return_value={
            "answers": {"c0": {"noul": .04}, "c1": {"noul": .98}}}) as call:
            selected = mutations.assignment_columns(
                "Change the value for entries whose label is Alpha.", program, columns)
        self.assertEqual(selected, ("amount",))
        self.assertEqual(len(call.call_args.args[1]), 2)

    def test_near_spelling_is_candidate_evidence_only(self):
        column = Column("category", "TEXT", True, False)
        candidates = value_candidates(
            "entries in the Comdey category", column, ("Drama", "Comedy"), ())
        matched = [item for item in candidates.values() if item["value"] == "Comedy"]
        self.assertEqual(matched[0]["evidence"]["kind"], "near_spelling")

    def test_filtered_multi_row_mutation_needs_confirmation_and_can_undo(self):
        condition = (Condition("date", "BETWEEN", ("2023-01-01", "2023-12-31")),)
        for operation, assignments in (("UPDATE", {"amount": 11}), ("DELETE", {})):
            with self.subTest(operation=operation):
                program = mutations.MutationProgram(
                    operation, "entries", assignments, target_mode="predicate",
                    conditions=condition)
                sql, params = mutations.compile_mutation(program, self.schema)
                with connect(self.path) as conn:
                    conn.row_factory = sqlite3.Row
                    before = mutations.inspect_rows(conn, program)
                    preview = mutations.preview_mutation(conn, program, sql, params, before)
                    self.assertEqual(preview["affected"], 2)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 4)
                plan = {"supported": True, "vet": {"passed": True},
                        "sql": sql, "params": params, "affected": 2,
                        "fingerprint": mutations.fingerprint(self.path),
                        "program": {"table": "entries"}}
                token = operation.lower()
                with patch.dict(web.PENDING, {token: {"database": "fixture", "plan": plan}}, clear=True), \
                     patch.dict(web.UNDO, {}, clear=True), \
                     patch.object(web, "database_path", return_value=self.path), \
                     patch.object(web, "BACKUP_DIR", Path(self.folder.name)), \
                     patch.object(web, "schema", return_value={}):
                    with self.assertRaises(HTTPException):
                        web.commit(web.CommitInput(token=token, confirm=False))
                    with connect(self.path) as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 4)
                    committed = web.commit(web.CommitInput(token=token, confirm=True))
                    self.assertEqual(committed["affected"], 2)
                    with self.assertRaises(HTTPException):
                        web.commit(web.CommitInput(token=token, confirm=True))
                    with connect(self.path) as conn:
                        if operation == "UPDATE":
                            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries WHERE amount=11").fetchone()[0], 2)
                        else:
                            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)
                    self.assertTrue(web.undo(web.UndoInput(token=token))["restored"])
                    with connect(self.path) as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 4)
                        self.assertEqual(conn.execute("SELECT amount FROM entries WHERE id=3").fetchone()[0], 9)

    def test_filtered_row_cap_and_stale_preview_reject(self):
        condition = (Condition("amount", ">=", 0),)
        program = mutations.MutationProgram("DELETE", "entries", target_mode="predicate",
                                            conditions=condition)
        sql, params = mutations.compile_mutation(program, self.schema)
        with connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            before = mutations.inspect_rows(conn, program)
            preview = mutations.preview_mutation(conn, program, sql, params, before)
        plan = {"supported": True, "vet": {"passed": True},
                "sql": sql, "params": params, "affected": preview["affected"],
                "fingerprint": mutations.fingerprint(self.path)}
        with connect(self.path) as conn:
            conn.execute("INSERT INTO entries VALUES (5, 'New', 1, '2024-01-01', 'Drama', NULL)")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            mutations.commit_mutation(self.path, plan)
        with connect(self.path) as conn:
            conn.executemany("INSERT INTO entries VALUES (?, 'Fill', 1, '2024-01-01', 'Drama', NULL)",
                             [(index,) for index in range(6, 106)])
        with connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            capped = mutations.inspect_rows(conn, program)
            self.assertEqual(len(capped), mutations.MAX_AFFECTED + 1)
            with self.assertRaisesRegex(ValueError, "Unsafe affected-row count"):
                mutations.preview_mutation(conn, program, sql, params, capped)


if __name__ == "__main__":
    unittest.main()
