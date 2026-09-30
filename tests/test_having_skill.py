"""HAVING operators and thresholds are separate bounded decisions."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.facts import extract_facts
from intentsql.skills.having import resolve_having


class HavingSkillTests(unittest.TestCase):
    def test_one_numeric_threshold_needs_only_operator_call(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"operator": {
            "choice": "<=", "probabilities": {"<=": .9}}}}, 120, 20, 300)
        request = "groups with at most 3 records"
        result = resolve_having(request, "category", "count", client)
        fact = next(item for item in extract_facts(request) if item.value == 3)
        self.assertEqual((result.operator, result.value, result.jev_calls), ("<=", 3, 1))
        self.assertEqual(result.fact_id, fact.fact_id)

    def test_chosen_threshold_keeps_its_own_fact_id(self):
        request = "From 1990 through 2005, keep groups whose maximum is at least 60."
        numbers = [fact for fact in extract_facts(request) if fact.kind == "integer"]
        chosen = next(fact for fact in numbers if fact.value == 60)
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"operator": {"choice": ">=",
                        "probabilities": {">=": .95}}}}, 80, 12, 100),
            CallResult({"answers": {"threshold": {"choice": f"n{numbers.index(chosen)}",
                        "probabilities": {f"n{numbers.index(chosen)}": .9}}}}, 40, 8, 100),
        ]
        result = resolve_having(request, "year", "maximum", client)
        self.assertEqual((result.operator, result.value, result.jev_calls), (">=", 60, 2))
        self.assertEqual(result.fact_id, chosen.fact_id)


if __name__ == "__main__":
    unittest.main()
