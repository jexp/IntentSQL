"""Boolean connectors come from bounded Jev choices."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.condition_logic import resolve_condition_logic


class ConditionLogicTests(unittest.TestCase):
    def test_two_conditions_require_one_choice(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"logic": {
            "choice": "OR", "probabilities": {"OR": .9}}}}, 120, 20, 300)
        result = resolve_condition_logic("A or B", ("a", "b"), client)
        self.assertEqual((result.connector, result.jev_calls), ("OR", 1))


    def test_atomic_negative_predicate_is_not_mistaken_for_nested_boolean(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({"answers": {"logic": {
                "choice": "complex", "confidence": .7,
                "probabilities": {"complex": .7, "AND": .3},
            }}}, 100, 12, 40),
            CallResult({"answers": {"logic": {
                "choice": "AND", "confidence": .97,
                "probabilities": {"AND": .97, "complex": .03},
            }}}, 90, 10, 30),
        ]
        evidence = (
            {"column": "city", "operator": "=", "value": "Boston"},
            {"column": "type", "operator": "!=", "value": "Public School"},
        )
        result = resolve_condition_logic(
            "Boston schools that aren't public schools", ("city", "type"), client, evidence)
        self.assertEqual((result.connector, result.status, result.jev_calls),
                         ("AND", "resolved", 2))

    def test_one_condition_uses_no_call(self):
        result = resolve_condition_logic("A", ("a",))
        self.assertEqual((result.connector, result.jev_calls), ("AND", 0))


if __name__ == "__main__":
    unittest.main()
