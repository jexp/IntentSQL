"""Exact alternatives retain literal types and never turn ranges into sets."""
import unittest
from unittest.mock import Mock

from intentsql.jev_client import CallResult
from intentsql.skills.alternatives import resolve_alternatives
from intentsql.skills.count_shape import resolve_count_shape
from intentsql.skills.facts import extract_facts
from intentsql.semantic_read import Condition, SelectQuery, compile_select


class AlternativeTests(unittest.TestCase):
    def client(self, relationship, scores):
        client = Mock()
        client.call.return_value = CallResult({'answers': {
            'relationship': {'choice': relationship},
            **{f'v{i}': {'noul': score} for i, score in enumerate(scores)}
        }}, 20, 10, 5)
        return client

    def test_numeric_alternatives_keep_types_and_bind_unknown_values(self):
        prompt = 'Count records with priority 2 or 999'
        result = resolve_alternatives(prompt, 'priority', (), self.client('alternatives', [.99,.98]),
                                      numeric_facts=extract_facts(prompt))
        self.assertEqual(result.values, (2,999))
        self.assertEqual(len(result.fact_ids),2)
        sql, params = compile_select(SelectQuery('records','count',conditions=(Condition('priority','IN',result.values),)), ('priority',))
        self.assertIn('IN (?, ?)',sql)
        self.assertEqual(params,[2,999])

    def test_a_role_word_does_not_invent_an_alternative_code(self):
        client = self.client("alternatives", [.99, .99])
        result = resolve_alternatives(
            "How many left-handed batters are in the players table?",
            "bats", ("B", "L", "R"), client)
        self.assertEqual(result.values, ())
        client.call.assert_not_called()

    def test_abbreviated_observed_alternatives_reach_one_bounded_call(self):
        result = resolve_alternatives(
            "born in Puerto Rico or Canada", "birth_country",
            ("P.R.", "CAN", "USA"),
            self.client("alternatives", [.99, .99]))
        self.assertEqual(result.values, ("P.R.", "CAN"))
        state = result.trace[0]["state"]
        self.assertEqual(
            [item["evidence"]["kind"] for item in state["retrieved_observed_values"]],
            ["request_initialism", "compact_observed_code_prefix"])

    def test_range_endpoints_are_not_alternatives(self):
        prompt = 'Count records with priority between 2 and 5'
        client = self.client('other',[.99,.99])
        result = resolve_alternatives(prompt,'priority',(),client,numeric_facts=extract_facts(prompt))
        self.assertEqual(result.values,())
        self.assertEqual(result.fact_ids,())
        client.call.assert_not_called()

    def test_interval_is_not_a_set_when_another_number_exists(self):
        prompt = "From 1990 through 2005, show years whose maximum was at least 60."
        client = Mock()
        result = resolve_alternatives(
            prompt, "year", (), client, numeric_facts=extract_facts(prompt))
        self.assertEqual(result.values, ())
        client.call.assert_not_called()

    def test_single_value_does_not_spend_a_call(self):
        client = Mock()
        self.assertEqual(resolve_alternatives('priority 2','priority',(),client,numeric_facts=extract_facts('priority 2')).values,())
        client.call.assert_not_called()

    def test_other_explicit_column_does_not_supply_numeric_alternative(self):
        prompt = 'Show records in season 2 with rating greater than 7'
        client = Mock()
        result = resolve_alternatives(
            prompt, 'season', (), client, numeric_facts=extract_facts(prompt),
            other_columns=('rating',))
        self.assertEqual(result.values, ())
        client.call.assert_not_called()

    def test_same_explicit_column_keeps_numeric_alternatives(self):
        prompt = 'Show records in season 1 or 2'
        result = resolve_alternatives(
            prompt, 'season', (), self.client('alternatives', [.9, .9]),
            numeric_facts=extract_facts(prompt), other_columns=('rating',))
        self.assertEqual(result.values, (1, 2))

    def test_unclear_alternatives_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'no partial query'):
            resolve_alternatives('red or blue','color',('red','blue'),self.client('alternatives',[.9,.2]))

    def test_count_shape_is_one_independent_decision(self):
        client=Mock()
        client.call.return_value=CallResult({'answers':{'shape':{'choice':'scalar'}}},20,10,5)
        result=resolve_count_shape('Count records with priority 2 or 5',('priority',),client)
        self.assertEqual(result.shape,'scalar')
        self.assertEqual(result.jev_calls,1)
        client.call.assert_called_once()


if __name__=='__main__':
    unittest.main()
