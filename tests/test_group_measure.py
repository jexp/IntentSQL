"""A grouped-key request resolves its measure without inventing SQL or schema."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.group_measure import resolve_group_measure


class GroupMeasureTests(unittest.TestCase):
    def test_single_call_combines_measure_and_threshold_decisions(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "measure": {"choice": "count", "probabilities": {"count": .9, "aggregate": .1}},
            "qualifier": {"noul": .85}}}, 90, 12, 100)
        result = resolve_group_measure("show matching groups", "records", ("category", "value"), client)
        self.assertEqual((result.kind, result.qualifies_groups, result.jev_calls),
                         ("count", True, 1))
        state, questions = client.call.call_args.args
        self.assertEqual(state["available_columns"], ("category", "value"))
        self.assertEqual(set(questions), {"measure", "qualifier"})

    def test_unknown_answer_fails_closed(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "measure": {"choice": "invented"}, "qualifier": {"noul": .5}}}, 90, 12, 100)
        with self.assertRaises(RuntimeError):
            resolve_group_measure("show groups", "records", ("category",), client)


if __name__ == "__main__":
    unittest.main()
