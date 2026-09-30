"""Value hints sample only a selected column with bound lexical parameters."""

from intentsql.database import connect
import unittest

from intentsql.skills.schema import columns_for
from intentsql.skills.value_hints import (mechanically_related_values,
                                          small_category_hints, targeted_values,
                                          profiled_text_patterns)


class ValueHintTests(unittest.TestCase):
    def test_initialisms_and_prefix_codes_are_retrieval_evidence(self):
        related = dict(mechanically_related_values(
            "records from Puerto Rico, Canada, or Domincan Republic",
            ("P.R.", "CAN", "D.R.", "USA", "France")))
        self.assertEqual(related["P.R."]["kind"], "request_initialism")
        self.assertEqual(related["CAN"]["kind"], "compact_observed_code_prefix")
        self.assertEqual(related["D.R."]["kind"], "request_initialism")
        self.assertNotIn("USA", related)
        self.assertNotIn("France", related)

    def test_one_character_enum_prefix_is_bounded_retrieval_evidence(self):
        related = dict(mechanically_related_values(
            "players who throw left-handed", ("L", "R")))
        self.assertEqual(related["L"]["kind"],
                         "compact_observed_single_code_prefix")
        self.assertNotIn("R", related)

    def test_a_column_role_word_is_not_evidence_for_its_first_letter(self):
        related = dict(mechanically_related_values(
            "How many left-handed batters are in the players table?",
            ("B", "L", "R"), column_name="bats"))
        self.assertNotIn("B", related)
        self.assertEqual(related["L"]["request_words"], ["left"])
        throwing = dict(mechanically_related_values(
            "Count players who throw left-handed.", ("L", "R"),
            column_name="throws"))
        self.assertEqual(list(throwing), ["L"])
        self.assertEqual(throwing["L"]["request_words"], ["left"])

    def test_recurring_parenthetical_annotation_is_profiled_without_interpretation(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE entities (name TEXT)")
            conn.executemany("INSERT INTO entities VALUES (?)", [
                ("Alpha (inactive)",), ("Beta (inactive)",), ("Gamma",),
                ("Delta (other)",),
            ])
            self.assertEqual(profiled_text_patterns(conn, "entities", "name"),
                             ("(inactive)",))

    def test_only_complete_small_text_domains_are_disclosed(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE records (kind TEXT, label TEXT, amount INTEGER)")
            conn.executemany("INSERT INTO records VALUES (?, ?, ?)",
                             [("A" if index % 2 else "B", f"Name {index}", index)
                              for index in range(12)])
            hints = small_category_hints(conn, "records", columns_for(conn, "records"))
        self.assertEqual(set(hints["kind"]), {"A", "B"})
        self.assertNotIn("label", hints)
        self.assertNotIn("amount", hints)

    def test_unique_values_from_a_small_table_are_not_sent_as_a_category(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE people (name TEXT)")
            conn.executemany("INSERT INTO people VALUES (?)", [(f"Name {index}",) for index in range(4)])
            hints = small_category_hints(conn, "people", columns_for(conn, "people"))
        self.assertEqual(hints, {})

    def test_related_values_are_found_beyond_initial_rows(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE lessons (topic TEXT)")
            conn.executemany("INSERT INTO lessons VALUES (?)",
                             [(f"Other {i}",) for i in range(100)]
                             + [("Fractions",), ("Equivalent Fractions",),
                                ("Fractions 101",)])
            values = targeted_values(conn, "lessons", "topic",
                                     "episodes teaching fractions")
        self.assertIn("Fractions", values)
        self.assertIn("Equivalent Fractions", values)
        self.assertIn("Fractions 101", values)

    def test_hyphenated_request_exposes_component_word(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE lessons (topic TEXT)")
            conn.executemany("INSERT INTO lessons VALUES (?)",
                             [("Other",), ("Fractions",), ("Equivalent Fractions",)])
            values = targeted_values(conn, "lessons", "topic", "fraction-related episodes")
        self.assertIn("Fractions", values)


    def test_compact_observed_code_is_prioritized_by_request_prefix(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE people (country TEXT)")
            conn.executemany("INSERT INTO people VALUES (?)",
                             [("USA",), ("MEX",), ("CAN",), ("France",)])
            values = targeted_values(conn, "people", "country",
                                     "Canadian-born players", limit=3)
        self.assertIn("CAN", values)

    def test_numeric_extrema_are_available_without_matching_literals(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE rates (graduated REAL)")
            conn.executemany("INSERT INTO rates VALUES (?)",
                             [(90 + index / 10,) for index in range(30)] + [(100.0,)])
            values = targeted_values(conn, "rates", "graduated", "perfect result",
                                     include_extrema=True)
        self.assertIn("100.0", values)

    def test_exact_value_survives_many_partial_matches(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE things (label TEXT)")
            conn.executemany("INSERT INTO things VALUES (?)",
                             [(f"Longer Alpha suffix {index}",) for index in range(40)] +
                             [("Alpha",)])
            hints = targeted_values(conn, "things", "label",
                                    "Find the item named Alpha", limit=3)
        self.assertEqual(hints[0], "Alpha")
        self.assertLessEqual(len(hints), 3)

    def test_exact_normalized_phrase_retrieves_raw_padded_stored_value(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE flights (destination TEXT)")
            conn.executemany("INSERT INTO flights VALUES (?)", [(" ATO",), ("BOS",)])
            hints = targeted_values(conn, "flights", "destination",
                                    "flights into ATO", limit=3)
        self.assertEqual(hints[0], " ATO")
        related = dict(mechanically_related_values("flights into ATO", hints))
        self.assertIn(" ATO", related)
        self.assertEqual(related[" ATO"]["kind"], "exact_request_phrase")

    def test_typo_retrieval_uses_only_a_complete_small_domain(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE places (region TEXT)")
            conn.executemany("INSERT INTO places VALUES (?)",
                             [("Northern Coast",), ("Southern Valley",),
                              ("Eastern Plain",), ("Western Plateau",)])
            hints = targeted_values(conn, "places", "region",
                                    "records in the Nortern Coast", limit=3)
        self.assertEqual(hints[0], "Northern Coast")


if __name__ == "__main__":
    unittest.main()
