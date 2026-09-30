"""Entity candidates are inspected schema names, never built-in DB mappings."""

from intentsql.database import connect
import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.entity import resolve_entity
from intentsql.skills.schema import columns_for, table_names


class EntitySkillTests(unittest.TestCase):
    def test_schema_is_inspected_mechanically(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, label TEXT)")
            conn.execute("CREATE TABLE shipments (id INTEGER)")
            self.assertEqual(table_names(conn), ("shipments", "widgets"))
            self.assertEqual([column.name for column in columns_for(conn, "widgets")],
                             ["id", "label"])

    def test_single_table_does_not_call_jev(self):
        client = Mock()
        result = resolve_entity("show some records", ("widgets",), client)
        self.assertEqual(result.table, "widgets")
        client.call.assert_not_called()

    def test_multiple_tables_offer_schema_names_and_unspecified(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"entity": {
            "choice": "t1", "confidence": .91,
            "probabilities": {"t0": .02, "t1": .95, "unspecified": .03}}}},
            300, 45, 650)
        result = resolve_entity("Which widgets?", ("shipments", "widgets"), client)
        self.assertEqual(result.table, "widgets")
        state, questions = client.call.call_args.args
        self.assertEqual(state["candidate_tables"], ("shipments", "widgets"))
        self.assertEqual(set(questions["entity"]["criteria"].values()),
                         {"shipments", "widgets", "No single source table is identified by the request"})

    def test_relationship_mode_selects_anchor_instead_of_requiring_one_table(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"entity": {
            "choice": "t1", "confidence": .94,
            "probabilities": {"t0": .04, "t1": .94, "unsupported": .02}}}},
            320, 40, 600)
        context = {
            "players": [{"table": "salaries", "columns": ["year", "salary"]}],
            "salaries": [{"table": "players", "columns": ["first_name", "birth_country"]}],
        }
        result = resolve_entity(
            "Show the highest salary records for players born in the USA",
            ("players", "salaries"), client,
            {"players": [{"name": "first_name"}],
             "salaries": [{"name": "salary"}]},
            relationship_mode=True, relationship_context=context)
        self.assertEqual(result.table, "salaries")
        state, questions = client.call.call_args.args
        self.assertEqual(state["candidate_direct_relationships"], context)
        self.assertNotIn("unspecified", questions["entity"]["criteria"])
        self.assertIn("unsupported", questions["entity"]["criteria"])


if __name__ == "__main__":
    unittest.main()
