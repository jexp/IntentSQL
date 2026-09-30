"""Extract calendar syntax without assigning a database role to it."""

import unittest

from intentsql.skills.facts import extract_facts


class FactTests(unittest.TestCase):
    def test_month_first_date_is_one_literal(self):
        facts = extract_facts("What aired on December 31, 2004?")
        self.assertEqual([(fact.kind, fact.value) for fact in facts],
                         [("date", "2004-12-31")])

    def test_day_first_and_abbreviated_month(self):
        facts = extract_facts("From 3 Jan 2020 to 31st December 2021")
        self.assertEqual([fact.value for fact in facts],
                         ["2020-01-03", "2021-12-31"])

    def test_invalid_calendar_date_is_not_normalized(self):
        facts = extract_facts("February 30, 2004 or 2024-13-01")
        self.assertEqual([fact.kind for fact in facts], ["invalid_date", "invalid_date"])
