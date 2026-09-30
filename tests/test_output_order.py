import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.output_order import resolve_output_order


class OutputOrderTests(unittest.TestCase):
    def test_explicit_selected_fields_follow_request_order_without_jev(self):
        client = Mock()
        result = resolve_output_order("show district then pupils",
                                      ("pupils", "district"), client)
        self.assertEqual(result.labels, ("district", "pupils"))
        self.assertEqual(result.jev_calls, 0)
        client.call.assert_not_called()

    def test_implicit_small_output_order_still_uses_bounded_choice(self):
        client = Mock()
        client.call.return_value = CallResult({"answers": {"order": {
            "choice": "o1", "confidence": .9, "probabilities": {"o1": .9}}}}, 40, 5, 1)
        result = resolve_output_order("show the requested attributes",
                                      ("pupils", "district"), client)
        self.assertEqual(result.labels, ("district", "pupils"))
        self.assertEqual(result.jev_calls, 1)

    def test_explicit_return_order_uses_one_remaining_slot_for_paraphrased_field(self):
        client = Mock()
        labels = ("season", "episode_in_season", "title")
        result = resolve_output_order(
            "Return season, title and episode number, ordered by season",
            labels, client)
        self.assertEqual(result.labels, ("season", "title", "episode_in_season"))
        self.assertEqual(result.jev_calls, 0)
        client.call.assert_not_called()

    def test_large_output_set_does_not_create_factorial_choices(self):
        client = Mock()
        labels = ("a", "b", "c", "d")
        self.assertEqual(resolve_output_order("show all", labels, client).labels, labels)
        client.call.assert_not_called()

    def test_four_explicit_fields_use_request_order_mechanically(self):
        client = Mock()
        labels = ("year", "salary", "first_name", "last_name")
        result = resolve_output_order(
            "Return first name, last name, year and salary", labels, client)
        self.assertEqual(result.labels, ("first_name", "last_name", "year", "salary"))
        client.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
