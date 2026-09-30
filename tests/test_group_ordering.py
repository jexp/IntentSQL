"""Grouped ordering distinguishes keys, metrics, and optional tie breaks."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.group_ordering import resolve_group_ordering


class GroupOrderingTests(unittest.TestCase):
    def test_metric_order_with_tie_break(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {
            "target": {"choice": "measure"}, "direction": {"choice": "DESC"},
            "tie": {"choice": "ASC"}}}, 200, 40, 300)
        result = resolve_group_ordering("highest count, name ties", "name", "count", client)
        self.assertEqual((result.target, result.direction, result.tie_key_direction),
                         ("measure", "DESC", "ASC"))


if __name__ == "__main__":
    unittest.main()
