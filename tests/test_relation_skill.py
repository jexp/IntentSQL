"""Only declared foreign keys can become join candidates."""

from intentsql.database import connect
import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.relation import resolve_related_table
from intentsql.skills.schema import direct_relations


class RelationSkillTests(unittest.TestCase):
    def test_schema_edges_are_bidirectionally_discoverable(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parents(id))")
            child = direct_relations(conn, "children")
            parent = direct_relations(conn, "parents")
        self.assertEqual(child, parent)
        self.assertEqual(child[0].other_table("parents"), "children")

    def test_one_direct_relation_needs_no_jev_call(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE children (parent_id INTEGER REFERENCES parents(id))")
            edges = direct_relations(conn, "children")
        client = Mock()
        result = resolve_related_table("names from parents", "children", edges, client)
        self.assertEqual(result.relation.other_table("children"), "parents")
        client.call.assert_not_called()

    def test_multiple_edges_use_one_bounded_choice(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE facts (a_id INTEGER REFERENCES a(id), b_id INTEGER REFERENCES b(id))")
            edges = direct_relations(conn, "facts")
        client = Mock()
        selected = f"r{next(index for index, edge in enumerate(edges) if edge.other_table('facts') == 'b')}"
        client.call.return_value = CallResult({"answers": {"relation": {
            "choice": selected, "probabilities": {selected: .9}}}}, 100, 20, 300)
        result = resolve_related_table("need b", "facts", edges, client)
        self.assertEqual(result.relation.other_table("facts"), "b")


if __name__ == "__main__":
    unittest.main()
