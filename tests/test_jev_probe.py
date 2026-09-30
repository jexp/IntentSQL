"""The probe must preserve independent answers and account for every call."""

import json
import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from tools.jev_probe import run_fixture


class ProbeTests(unittest.TestCase):
    def test_repeats_and_records_usage_without_connection_details(self):
        response = {"answers": {"n": {"choice": "two", "confidence": 0.8,
                                      "probabilities": {"two": 0.8, "three": 0.2}}}}
        client = Mock()
        client.call.return_value = CallResult(response, 11, 3, 20)
        fixture = {"cases": [{"name": "couple", "state": {"request": "a couple"},
                              "questions": {"n": {"type": "choice"}},
                              "expected": {"n": "two"}}]}
        result = run_fixture(fixture, repeat=3, client=client)
        self.assertEqual(client.call.call_count, 3)
        self.assertEqual(result["totals"], {"calls": 3, "input_tokens": 33,
                                             "output_tokens": 9, "correct": 3, "checks": 3})
        self.assertEqual(result["runs"][0]["selected"], {"n": "two"})
        self.assertNotIn("api_key", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
