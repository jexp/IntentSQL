"""Inclusive ranges use extracted facts and observed column format."""

import sqlite3

from intentsql.database import connect
import unittest

from intentsql.skills.facts import extract_facts
from intentsql.skills.range_value import (column_uses_iso_dates,
                                          resolve_range_bounds, strict_date_bounds)


class RangeValueTests(unittest.TestCase):
    def test_after_before_dates_remain_strict(self):
        request = "after January 1st 2020 and before January 1st 2023"
        result = strict_date_bounds(request, extract_facts(request))
        self.assertEqual((result.status, result.lower, result.upper),
                         ("resolved", "2020-01-01", "2023-01-01"))
        inclusive = "from January 1st 2020 through January 1st 2023"
        self.assertEqual(strict_date_bounds(
            inclusive, extract_facts(inclusive)).status, "none")

    def test_years_expand_only_for_iso_date_columns(self):
        with connect(":memory:") as conn:
            conn.execute("CREATE TABLE events (occurred NUMERIC, amount INTEGER)")
            conn.execute("INSERT INTO events VALUES ('2020-05-03', 35)")
            self.assertTrue(column_uses_iso_dates(conn, "events", "occurred"))
            self.assertFalse(column_uses_iso_dates(conn, "events", "amount"))
        dates = resolve_range_bounds("between 2018 and 2023", "occurred", True)
        self.assertEqual((dates.lower, dates.upper), ("2018-01-01", "2023-12-31"))

    def test_iso_datetime_values_have_date_semantics(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE events (occurred DATETIME)")
        connection.executemany("INSERT INTO events VALUES (?)", [
            ("2020-01-02 03:04:05",), ("2021-02-03 04:05:06",)])
        self.assertTrue(column_uses_iso_dates(connection, "events", "occurred"))
        numbers = resolve_range_bounds("between 18 and 23", "amount", False)
        self.assertEqual((numbers.lower, numbers.upper), (18, 23))


    def test_month_and_year_expand_to_exact_calendar_month(self):
        february = resolve_range_bounds("during February 2026", "occurred", True)
        self.assertEqual((february.lower, february.upper),
                         ("2026-02-01", "2026-02-28"))
        self.assertEqual(february.source, "single_month_on_iso_date_column")

        leap_february = resolve_range_bounds("during Feb 2024", "occurred", True)
        self.assertEqual((leap_february.lower, leap_february.upper),
                         ("2024-02-01", "2024-02-29"))

        range_result = resolve_range_bounds(
            "from January through September 2023", "occurred", True)
        self.assertEqual((range_result.lower, range_result.upper),
                         ("2023-01-01", "2023-09-30"))

    def test_one_year_is_not_confused_with_other_numeric_condition(self):
        bounds = resolve_range_bounds("season 6 during 2007", "occurred", True)
        self.assertEqual((bounds.lower, bounds.upper), ("2007-01-01", "2007-12-31"))


    def test_compact_magnitude_bounds_are_exact_literals(self):
        bounds = resolve_range_bounds("between 30k and 36k", "amount", False)
        self.assertEqual((bounds.lower, bounds.upper), (30000, 36000))
        self.assertEqual(bounds.source, "two_literal_facts")

    def test_missing_second_bound_fails_closed(self):
        result = resolve_range_bounds("above 18", "amount", False)
        self.assertEqual(result.status, "ambiguous")


if __name__ == "__main__":
    unittest.main()
