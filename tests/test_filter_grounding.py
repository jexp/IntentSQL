"""An uncertain route cannot invent a WHERE clause or silently drop one."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from intentsql.database import connect
from intentsql.jev_client import CallResult
from intentsql.semantic_read import run_read


def client_with_filter_review(review_score):
    client = Mock()

    def answer(_state, questions):
        if "output" in questions or "quantity" in questions:
            values = {name: {"noul": 0.0} for name in
                      ("filters", "ordering", "distinct", "grouping", "relationship",
                       "having", "expression", "windowing")}
            values.update(output={"choice": "count"}, quantity={"choice": "none"},
                          filters={"noul": .9})
        elif "next_predicate" in questions:
            values = {"next_predicate": {"choice": "none"}}
        elif "column" in questions:
            values = {"column": {"choice": "none"}}
        elif "filter" in questions:
            values = {"filter": {"noul": review_score}}
        elif "coverage" in questions:
            values = {"coverage": {"choice": "complete"}}
        elif all(key.startswith("c") for key in questions):
            values = {key: {"noul": 0.0} for key in questions}
        else:
            raise AssertionError(f"Unexpected semantic call: {tuple(questions)}")
        return CallResult({"answers": values}, 20, 4, 1)

    client.call.side_effect = answer
    return client


class FilterGroundingTests(unittest.TestCase):
    def test_false_positive_filter_route_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE records (id INTEGER, category TEXT)")
                conn.executemany("INSERT INTO records VALUES (?, ?)",
                                 [(1, "A"), (2, "A"), (3, "B")])
            result = run_read(path, "How many records exist?", client_with_filter_review(.05))
            self.assertEqual(result["sql"], 'SELECT COUNT(*) AS row_count FROM "records"')
            self.assertEqual(result["rows"], [(3,)])
            self.assertIn("Source filter review", [step["name"] for step in result["steps"]])

    def test_real_unresolved_filter_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.db"
            with connect(path) as conn:
                conn.execute("CREATE TABLE records (id INTEGER, category TEXT)")
                conn.execute("INSERT INTO records VALUES (1, 'A')")
            with self.assertRaisesRegex(ValueError, "condition columns are unclear"):
                run_read(path, "How many qualifying records exist?",
                         client_with_filter_review(.95))


if __name__ == "__main__":
    unittest.main()
