"""Explicit surface routing only handles unmistakable operations."""

import unittest

from intentsql.request_routing import explicit_request_operation


class RequestRoutingTests(unittest.TestCase):
    def test_explicit_read_forms_skip_model_routing(self):
        self.assertEqual(explicit_request_operation("show all episodes"), ("read", None))
        self.assertEqual(explicit_request_operation("How many players are there?"), ("read", None))
        self.assertEqual(explicit_request_operation("please list schools"), ("read", None))

    def test_explicit_mutations_preserve_operation(self):
        self.assertEqual(explicit_request_operation("Delete all episodes from 2023"),
                         ("change", "DELETE"))
        self.assertEqual(explicit_request_operation("update employees set salary to 5"),
                         ("change", "UPDATE"))
        self.assertEqual(explicit_request_operation("Please insert a new record"),
                         ("change", "INSERT"))

    def test_ambiguous_forms_fall_back_to_jev(self):
        self.assertIsNone(explicit_request_operation("Can you remove duplicates?"))
        self.assertIsNone(explicit_request_operation("Add salary and bonus"))
        self.assertIsNone(explicit_request_operation("Create a summary of salaries"))


if __name__ == "__main__":
    unittest.main()
