"""Per-group row selection is bounded to an inspected MIN/MAX shape."""

import unittest
from types import SimpleNamespace

from intentsql.skills.per_group_extremum import (resolve_per_group_extremum,
                                                   should_probe_per_group_shape)
from intentsql.skills.schema import Column


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def call(self, state, questions):
        self.calls.append((state, questions))
        return SimpleNamespace(answers=self.answers, input_tokens=30,
                               output_tokens=8, elapsed_ms=4)


class PerGroupExtremumTests(unittest.TestCase):
    def setUp(self):
        self.columns = tuple(Column(name, declared, True, False)
                             for name, declared in (("season", "INTEGER"),
                                                    ("episode_in_season", "INTEGER"),
                                                    ("title", "TEXT")))

    def test_resolves_group_target_and_direction_from_schema_choices(self):
        client = FakeClient({
            "shape": {"choice": "per_group_extremum"},
            "group": {"choice": "c0"},
            "extremum": {"choice": "c1"},
            "direction": {"choice": "MIN"},
        })
        result = resolve_per_group_extremum(
            "Return the first episode in every season", "episodes", self.columns, client,
            iso_date_columns=("title",))
        self.assertEqual((result.group_column, result.extremum_column, result.function),
                         ("season", "episode_in_season", "MIN"))
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.shape, "per_group_extremum")
        self.assertNotIn("episodes", str(client.calls[0][1]))
        self.assertEqual(client.calls[0][0]["inspected_columns"]["c2"]["observed_format"],
                         "ISO date")


    def test_plain_language_signal_bypasses_false_negative_route(self):
        self.assertTrue(should_probe_per_group_shape(
            "Return the first episode from each season.", "rows",
            grouping_hint=False, windowing_hint=False))
        self.assertTrue(should_probe_per_group_shape(
            "Show me one episode from each season.", "rows",
            grouping_hint=False, windowing_hint=False))
        self.assertFalse(should_probe_per_group_shape(
            "Show episodes from season 2.", "rows",
            grouping_hint=False, windowing_hint=False))
        self.assertFalse(should_probe_per_group_shape(
            "Show the five districts with the highest per-pupil expenditure.",
            "fields", grouping_hint=False, windowing_hint=True))
        self.assertTrue(should_probe_per_group_shape(
            "Show the highest salary per team.", "fields",
            grouping_hint=True, windowing_hint=False))

    def test_one_per_group_uses_single_primary_key_as_deterministic_representative(self):
        columns = (Column("id", "INTEGER", True, True), *self.columns)
        client = FakeClient({
            "shape": {"choice": "per_group_representative"},
            "group": {"choice": "c1"},
            "extremum": {"choice": "c2"},
            "direction": {"choice": "MAX"},
        })
        result = resolve_per_group_extremum(
            "Show me one episode from each season", "episodes", columns, client)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.shape, "per_group_representative")
        self.assertEqual((result.group_column, result.extremum_column, result.function),
                         ("season", "id", "MIN"))


    def test_semantic_group_alias_can_validate_representative_scope(self):
        columns = (
            Column("id", "INTEGER", True, True),
            Column("first_name", "TEXT", True, False),
            Column("bats", "TEXT", True, False),
            Column("throws", "TEXT", True, False),
        )

        class SequenceClient(FakeClient):
            def call(self, state, questions):
                self.calls.append((state, questions))
                if len(self.calls) == 1:
                    answers = {
                        "shape": {"choice": "per_group_representative"},
                        "group": {"choice": "c2"},
                        "extremum": {"choice": "c1"},
                        "direction": {"choice": "MAX"},
                    }
                else:
                    answers = {"scope": {
                        "choice": "same_group", "confidence": .96,
                        "probabilities": {"same_group": .96, "different_scope": .04},
                    }}
                return SimpleNamespace(answers=answers, input_tokens=30,
                                       output_tokens=8, elapsed_ms=4)

        client = SequenceClient({})
        result = resolve_per_group_extremum(
            "Show one player from each batting side.", "players", columns, client)
        self.assertEqual((result.status, result.shape),
                         ("resolved", "per_group_representative"))
        self.assertEqual((result.group_column, result.extremum_column, result.function),
                         ("bats", "id", "MIN"))
        self.assertEqual(result.jev_calls, 2)
        self.assertEqual(client.calls[1][0]["selected_group_column"], "bats")

    def test_general_window_shape_fails_closed(self):
        client = FakeClient({
            "shape": {"choice": "unsupported"},
            "group": {"choice": "c0"},
            "extremum": {"choice": "c1"},
            "direction": {"choice": "MAX"},
        })
        result = resolve_per_group_extremum(
            "Rank five rows within each category", "records", self.columns, client)
        self.assertEqual(result.status, "unsupported")
        self.assertIsNone(result.group_column)

    def test_grouped_count_is_a_recognized_non_window_shape(self):
        client = FakeClient({
            "shape": {"choice": "grouped_count"},
            "group": {"choice": "c0"},
            "extremum": {"choice": "c1"},
            "direction": {"choice": "MAX"},
        })
        result = resolve_per_group_extremum(
            "return each category with its row count", "records", self.columns, client)
        self.assertEqual((result.status, result.shape), ("resolved", "grouped_count"))
        self.assertIsNone(result.extremum_column)

    def test_conflicting_aggregate_route_gets_one_focused_output_grain_review(self):
        class SequenceClient(FakeClient):
            def __init__(self, grain):
                super().__init__({})
                self.grain = grain

            def call(self, state, questions):
                self.calls.append((state, questions))
                answers = ({"shape": {"choice": "per_group_extremum"},
                            "group": {"choice": "c0"},
                            "extremum": {"choice": "c1"},
                            "direction": {"choice": "MIN"}} if len(self.calls) == 1
                           else {"grain": {"choice": self.grain, "confidence": .95}})
                return SimpleNamespace(answers=answers, input_tokens=30,
                                       output_tokens=8, elapsed_ms=4)

        for grain, expected in (("grouped_value", "grouped_aggregate"),
                                ("source_rows", "per_group_extremum"),
                                ("ambiguous", "unsupported")):
            with self.subTest(grain=grain):
                client = SequenceClient(grain)
                result = resolve_per_group_extremum(
                    "For each season, show the minimum episode number.",
                    "episodes", self.columns, client, aggregate_route_hint=True)
                self.assertEqual(result.shape, expected)
                self.assertEqual(result.jev_calls, 2)
                self.assertEqual(client.calls[1][0]["established_group"], "season")
                self.assertEqual(client.calls[1][0]["established_extremum"]["column"],
                                 "episode_in_season")

    def test_spurious_group_route_can_resolve_global_or_ordinary_rows(self):
        for shape in ("global_extremum", "ordinary_rows"):
            with self.subTest(shape=shape):
                client = FakeClient({
                    "shape": {"choice": shape}, "group": {"choice": "c0"},
                    "extremum": {"choice": "c1"}, "direction": {"choice": "MIN"},
                })
                result = resolve_per_group_extremum(
                    "show matching rows", "records", self.columns, client)
                self.assertEqual((result.status, result.shape), ("resolved", shape))
                self.assertIsNone(result.group_column)

    def test_per_unit_phrase_gets_bounded_scope_review(self):
        class SequenceClient(FakeClient):
            def call(self, state, questions):
                self.calls.append((state, questions))
                if len(self.calls) == 1:
                    answers = {"shape": {"choice": "per_group_extremum"},
                               "group": {"choice": "c0"},
                               "extremum": {"choice": "c1"},
                               "direction": {"choice": "MIN"}}
                elif len(self.calls) == 2:
                    answers = {"scope": {"choice": "different_scope",
                                           "confidence": .95,
                                           "probabilities": {"different_scope": .96,
                                                             "same_group": .04}}}
                else:
                    answers = {"cardinality": {"choice": "top_one", "confidence": .95,
                                                "probabilities": {"top_one": .97, "all": .03}}}
                return SimpleNamespace(answers=answers, input_tokens=30,
                                       output_tokens=8, elapsed_ms=4)
        client = SequenceClient({})
        result = resolve_per_group_extremum(
            "Find the record with the lowest cost for each unit",
            "records", self.columns, client)
        self.assertEqual(result.shape, "global_extremum")
        self.assertEqual(result.jev_calls, 3)


if __name__ == "__main__":
    unittest.main()
