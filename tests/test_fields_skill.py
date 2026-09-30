"""Output-field selection is grounded in the chosen table's schema."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.fields import resolve_distinct_target, resolve_fields
from intentsql.skills.schema import Column


class FieldsSkillTests(unittest.TestCase):
    def test_only_column_is_mechanical(self):
        client = Mock()
        result = resolve_fields("show the value", "measurements",
                                (Column("value", "TEXT", True, False),), client)
        self.assertEqual(result.columns, ("value",))
        client.call.assert_not_called()

    def test_multiple_columns_use_one_grounded_call(self):
        columns = (Column("id", "INTEGER", False, True),
                   Column("name", "TEXT", True, False),
                   Column("age", "INTEGER", True, False))
        client = Mock()
        client.call.side_effect = [CallResult({"answers": {
            "c0": {"noul": .1}, "c1": {"noul": .95}, "c2": {"noul": .9},
            "output_scope": {"choice": "selected_fields"}, "entity_rows": {"noul": .2}, "entity_label": {"choice": "none"}}},
            320, 50, 650), CallResult({"answers": {"order": {"choice": "o0"}}},
            40, 5, 50)]
        result = resolve_fields("show names and ages", "people", columns, client)
        self.assertEqual(result.columns, ("name", "age"))
        self.assertEqual(result.jev_calls, 2)
        state, questions = client.call.call_args_list[0].args
        self.assertEqual(state["candidate_output_columns"], ["id", "name", "age"])
        self.assertEqual(set(questions), {"c0", "c1", "c2",
                                          "entity_rows", "entity_label", "output_scope"})

    def test_entity_word_can_add_human_readable_label(self):
        columns = (Column("id", "INTEGER", False, True),
                   Column("name", "TEXT", True, False),
                   Column("price", "REAL", True, False))
        client = Mock()
        client.call.side_effect = [CallResult({"answers": {
            "c0": {"noul": .05}, "c1": {"noul": .35}, "c2": {"noul": .98},
            "output_scope": {"choice": "selected_fields"}, "entity_rows": {"noul": .93}, "entity_label": {"choice": "c1"}}},
            340, 60, 680), CallResult({"answers": {"order": {"choice": "o0"}}},
            40, 5, 50)]
        result = resolve_fields("Show the five cheapest products with their prices",
                                "products", columns, client)
        self.assertEqual(result.columns, ("name", "price"))
        self.assertEqual(result.jev_calls, 2)


    def test_explicit_entity_rows_override_filter_field_mentions(self):
        columns = (Column("id", "INTEGER", False, True),
                   Column("title", "TEXT", True, False),
                   Column("air_date", "TEXT", True, False))
        client = Mock()
        result = resolve_fields(
            "Show episodes with Hacker in the title that aired before 2010",
            "episodes", columns, client)
        self.assertTrue(result.all_columns)
        self.assertEqual(result.source, "explicit_entity_rows")
        client.call.assert_not_called()

    def test_entity_name_followed_by_requested_field_is_not_forced_to_rows(self):
        columns = (Column("id", "INTEGER", False, True),
                   Column("title", "TEXT", True, False))
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "c0": {"noul": .05}, "c1": {"noul": .95},
            "output_scope": {"choice": "selected_fields"},
            "entity_rows": {"noul": .1}, "entity_label": {"choice": "none"}}},
            100, 10, 50)
        result = resolve_fields("Show episode title", "episodes", columns, client)
        self.assertEqual(result.columns, ("title",))

    def test_plural_attribute_after_entity_name_is_not_forced_to_rows(self):
        columns = (Column("id", "INTEGER", False, True),
                   Column("title", "TEXT", True, False),
                   Column("air_date", "TEXT", True, False))
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "c0": {"noul": .02}, "c1": {"noul": .98}, "c2": {"noul": .02},
            "output_scope": {"choice": "selected_fields"},
            "entity_rows": {"noul": .08}, "entity_label": {"choice": "none"}}},
            120, 20, 50)
        result = resolve_fields(
            "Show episode titles ending with Trouble, alphabetically.",
            "episodes", columns, client)
        self.assertFalse(result.all_columns)
        self.assertEqual(result.columns, ("title",))

    def test_distinct_count_uses_one_choice(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "c1", "probabilities": {"c1": .9}},
            "count_grain": {"choice": "distinct_values"}}},
            200, 30, 500)
        result = resolve_distinct_target("count unique names", "people", (
            Column("id", "INTEGER", False, True),
            Column("name", "TEXT", True, False)), client)
        self.assertEqual(result.columns, ("name",))
        self.assertEqual(result.jev_calls, 1)


if __name__ == "__main__":
    unittest.main()
