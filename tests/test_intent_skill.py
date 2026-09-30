"""Semantic route is one independent Jev call with typed branch decisions."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.intent import route_intent


class IntentTests(unittest.TestCase):
    def test_route_returns_only_relevant_branches(self):
        answers = {
            "output": {"choice": "rows", "confidence": .9, "probabilities": {"rows": .9}},
            "quantity": {"choice": "numeric", "confidence": .9, "probabilities": {"numeric": .9}},
            "filters": {"noul": .9}, "ordering": {"noul": .1},
            "distinct": {"noul": .2}, "grouping": {"noul": .1},
            "relationship": {"noul": .1}, "having": {"noul": .1},
            "expression": {"noul": .1}, "windowing": {"noul": .1},
        }
        client = Mock()
        client.call.return_value = CallResult({"answers": answers}, 420, 90, 700)
        result = route_intent("show five customers from somewhere", client)
        self.assertEqual(result.relevant_branches,
                         ("entity", "predicate", "quantity"))
        self.assertEqual(result.input_tokens, 420)
        self.assertEqual(client.call.call_count, 1)
        state, questions = client.call.call_args.args
        self.assertEqual(state["request"], "show five customers from somewhere")
        self.assertEqual(len(questions), 10)
        self.assertIn("output", questions)
        self.assertNotIn("established_output_kind", state)

    def test_partitioned_ranking_routes_to_windowing(self):
        answers = {
            "output": {"choice": "fields", "confidence": .9, "probabilities": {"fields": .9}},
            "quantity": {"choice": "numeric", "confidence": .9, "probabilities": {"numeric": .9}},
            "filters": {"noul": .1}, "ordering": {"noul": .9},
            "distinct": {"noul": .1}, "grouping": {"noul": .2},
            "relationship": {"noul": .1}, "having": {"noul": .1},
            "expression": {"noul": .1}, "windowing": {"noul": .97},
        }
        client = Mock()
        client.call.return_value = CallResult({"answers": answers}, 450, 90, 700)
        result = route_intent(
            "Rank every employee by salary within their department and show the top two ranks from each department.",
            client)
        self.assertTrue(result.windowing)
        self.assertIn("windowing", result.relevant_branches)


if __name__ == "__main__":
    unittest.main()
