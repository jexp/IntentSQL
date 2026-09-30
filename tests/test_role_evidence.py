"""Column-name stems are role evidence, not predicates."""
import unittest

from intentsql.skills.role_evidence import explicitly_names_column, role_words


class RoleEvidenceTests(unittest.TestCase):
    def test_batting_and_throwing_words_ground_different_columns(self):
        batter = "How many left-handed batters are in the players table?"
        self.assertEqual(role_words(batter, "bats"), ("batters",))
        self.assertEqual(role_words(batter, "throws"), ())
        self.assertEqual(role_words("Count players who bat left-handed.", "bats"), ("bat",))
        self.assertEqual(role_words("Count players who throw left-handed.", "throws"),
                         ("throw",))
        self.assertEqual(role_words("How many left-handed hitters are recorded?", "bats"), ())
        self.assertFalse(explicitly_names_column(batter, "bats"))
        self.assertTrue(explicitly_names_column("where bats = L", "bats"))

    def test_distinct_role_words_do_not_overlap(self):
        request = "Count players who bat left-handed and throw left-handed."
        self.assertFalse(set(role_words(request, "bats")) & set(role_words(request, "throws")))
