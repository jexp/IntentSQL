"""Group thresholds select only registered deterministic measures."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.having_measure import resolve_having_measure


class HavingMeasureTests(unittest.TestCase):
    def test_row_count_is_a_registered_separate_measure(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"measure": {
            "choice": "row_count", "confidence": .98}}}, 20, 4, 1)
        result = resolve_having_measure(
            "groups with at least 20 records", "category", "AVG of amount", client)
        self.assertEqual((result.kind, result.status), ("row_count", "resolved"))
        criteria = client.call.call_args.args[1]["measure"]["criteria"]
        self.assertEqual(set(criteria),
                         {"output_measure", "row_count", "none", "unsupported"})


if __name__ == "__main__":
    unittest.main()
