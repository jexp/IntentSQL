"""Group keys come only from the selected table."""

import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.grouping import resolve_group_key
from intentsql.skills.schema import Column


class GroupingSkillTests(unittest.TestCase):
    def test_one_schema_grounded_choice(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"group": {
            "choice": "c1", "probabilities": {"c1": .9}}}}, 180, 25, 350)
        result = resolve_group_key("count by category", "items", (
            Column("id", "INT", False, True), Column("category", "TEXT", True, False)), client)
        self.assertEqual((result.column, result.jev_calls), ("category", 1))


if __name__ == "__main__":
    unittest.main()
