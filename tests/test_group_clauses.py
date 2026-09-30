"""Grouped result clauses distinguish ordering from thresholds."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.group_clauses import resolve_group_clauses, threshold_candidates


class GroupClausesTests(unittest.TestCase):
    def test_source_interval_literals_are_not_having_thresholds(self):
        self.assertEqual(threshold_candidates("Between 1990 and 2005, which year had the highest maximum total?"), ())
        remaining = threshold_candidates(
            "From 1990 through 2005, show years with at least 20 entries")
        self.assertEqual([item["value"] for item in remaining], [20])
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "threshold_fact": {"choice": "none"}, "having": {"noul": .99}, "ordering": {"noul": .99},
            "ordering_basis": {"choice": "measure"}}}, 10, 4, 1)
        result = resolve_group_clauses(
            "Between 1990 and 2005, which year had the highest maximum total?",
            "year", "MAX total", client)
        self.assertFalse(result.having)

    def test_explicit_measure_ranking_survives_low_ordering_noul(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "threshold_fact": {"choice": "none"}, "having": {"noul": .02},
            "ordering": {"noul": .35},
            "ordering_basis": {
                "choice": "measure", "confidence": .95,
                "probabilities": {"measure": .95, "none": .05},
            },
        }}, 100, 15, 50)
        result = resolve_group_clauses(
            "Which year had the highest maximum home-run total?",
            "year", "MAX of HR per group", client)
        self.assertFalse(result.having)
        self.assertTrue(result.ordering)

    def test_plain_grouping_does_not_gain_ordering(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "threshold_fact": {"choice": "none"}, "having": {"noul": .01},
            "ordering": {"noul": .08},
            "ordering_basis": {
                "choice": "none", "confidence": .95,
                "probabilities": {"none": .95, "measure": .05},
            },
        }}, 100, 15, 50)
        result = resolve_group_clauses(
            "Count schools by type", "type", "count of source rows per group", client)
        self.assertFalse(result.ordering)


if __name__ == "__main__":
    unittest.main()
