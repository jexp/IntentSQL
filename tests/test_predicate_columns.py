"""Independent column roles do not compete or become facts from mentions."""
import unittest
from unittest.mock import Mock
from intentsql.jev_client import CallResult
from intentsql.skills.predicate_columns import resolve_predicate_columns
from intentsql.skills.schema import Column


class PredicateColumnsTests(unittest.TestCase):
    def client(self, *scores):
        client = Mock()
        client.call.return_value = CallResult({'answers': {
            f'c{i}': {'noul': score} for i, score in enumerate(scores)}}, 20, 3, 1)
        return client

    def columns(self, *names):
        return tuple(Column(name, 'TEXT', True, False) for name in names)

    def test_independent_restrictions_share_one_call(self):
        client = self.client(.99, .01, .98)
        result = resolve_predicate_columns('constrain zone and status; return label', 'entries',
                                          self.columns('zone', 'label', 'status'), client)
        self.assertEqual((result.columns, result.jev_calls), (('zone', 'status'), 1))

    def test_exact_observed_mention_does_not_force_a_predicate(self):
        client = self.client(.01, .01)
        result = resolve_predicate_columns('return category and name', 'entries',
            self.columns('category', 'name'), client, value_evidence={'name': ('category',)})
        self.assertEqual(result.columns, ())
        self.assertEqual(result.status, 'resolved')

    def test_ambiguous_tail_preserves_established_column(self):
        result = resolve_predicate_columns('ambiguous', 'entries', self.columns('zone', 'label'),
                                          self.client(.5), established_columns=('zone',))
        self.assertEqual((result.columns, result.status), (('zone',), 'uncertain_tail'))
        self.assertEqual(result.uncertain_columns, ('label',))

    def test_uninspected_established_column_rejects(self):
        with self.assertRaises(ValueError):
            resolve_predicate_columns('request', 'entries', self.columns('label'),
                                      self.client(), established_columns=('invented',))

    def test_both_columns_can_be_filtered_by_the_same_category(self):
        client = Mock()
        client.call.side_effect = [
            CallResult({'answers': {'c0': {'noul': .98}, 'c1': {'noul': .98}}}, 20, 3, 1),
            CallResult({'answers': {'column': {'choice': 'multiple', 'confidence': .91}}}, 8, 2, 1),
        ]
        result = resolve_predicate_columns('both labels equal X', 'entries',
            self.columns('label', 'other_label'), client,
            value_evidence={'label': ('X',), 'other_label': ('X',)})
        self.assertEqual(result.columns, ('label', 'other_label'))
        self.assertEqual(client.call.call_count, 2)
        self.assertEqual(client.call.call_args_list[1].args[1]['column']['criteria']['multiple'][:4],
                         'Each')

    def _role_choice(self, scores, choice, confidence=.9):
        client = Mock()
        client.call.side_effect = [
            CallResult({'answers': {f'c{i}': {'noul': score}
                                    for i, score in enumerate(scores)}}, 20, 3, 1),
            CallResult({'answers': {'column': {'choice': choice, 'confidence': confidence}}},
                       8, 2, 1),
        ]
        return client

    def test_overlapping_handedness_codes_choose_one_role(self):
        client = self._role_choice((.82, .74), 'c0', .9)
        result = resolve_predicate_columns(
            'How many left-handed batters are in the players table?', 'players',
            self.columns('bats', 'throws'), client,
            value_evidence={'bats': ('B', 'L', 'R'), 'throws': ('L', 'R')})
        self.assertEqual(result.columns, ('bats',))
        self.assertEqual(result.uncertain_columns, ())
        criteria = client.call.call_args_list[1].args[1]['column']['criteria']
        self.assertEqual(criteria['c0']['request_words_sharing_the_column_name'], ('batters',))
        self.assertEqual(criteria['c1']['request_words_sharing_the_column_name'], ())
        self.assertNotIn('throws', result.columns)

    def test_throwing_wording_chooses_the_throwing_column_only(self):
        client = self._role_choice((.8, .86), 'c1', .88)
        result = resolve_predicate_columns(
            'Count players who throw left-handed.', 'players',
            self.columns('bats', 'throws'), client,
            value_evidence={'bats': ('L', 'R', 'B'), 'throws': ('L', 'R')})
        self.assertEqual(result.columns, ('throws',))

    def test_distinct_role_words_do_not_ask_for_a_second_choice(self):
        client = self.client(.95, .96)
        result = resolve_predicate_columns(
            'Count players who bat left-handed and throw left-handed.', 'players',
            self.columns('bats', 'throws'), client,
            value_evidence={'bats': ('L', 'R'), 'throws': ('L', 'R')})
        self.assertEqual(result.columns, ('bats', 'throws'))
        self.assertEqual(client.call.call_count, 1)

    def test_unconfident_choice_does_not_keep_two_unnamed_columns(self):
        client = self._role_choice((.84, .81), 'c0', .42)
        result = resolve_predicate_columns(
            'How many lefties are recorded?', 'players',
            self.columns('hand', 'arm'), client,
            value_evidence={'hand': ('L', 'R'), 'arm': ('L', 'R')})
        self.assertEqual(result.columns, ())
        self.assertEqual(result.status, 'ambiguous')
        self.assertEqual(result.uncertain_columns, ('hand', 'arm'))

    def test_unconfident_choice_keeps_one_selected_column(self):
        client = self._role_choice((.91, .44), 'c1', .3)
        result = resolve_predicate_columns(
            'How many left-handed people are recorded?', 'players',
            self.columns('hand', 'arm'), client,
            value_evidence={'hand': ('L', 'R'), 'arm': ('L', 'R')})
        self.assertEqual(result.columns, ('hand',))
        self.assertEqual(result.uncertain_columns, ())

    def test_unconfident_choice_keeps_the_column_named_by_the_request(self):
        client = self._role_choice((.8, .79), 'c1', .4)
        result = resolve_predicate_columns(
            'How many left-handed batters are in the players table?', 'players',
            self.columns('bats', 'throws'), client,
            value_evidence={'bats': ('B', 'L', 'R'), 'throws': ('L', 'R')})
        self.assertEqual(result.columns, ('bats',))

    def test_confident_none_adds_neither_overlapping_column(self):
        client = self._role_choice((.77, .73), 'none', .8)
        result = resolve_predicate_columns(
            'How many lefties are recorded?', 'players',
            self.columns('hand', 'arm'), client,
            value_evidence={'hand': ('L', 'R'), 'arm': ('L', 'R')})
        self.assertEqual(result.columns, ())
        self.assertEqual(result.status, 'resolved')
        self.assertEqual(result.uncertain_columns, ())

    def test_partial_domain_overlap_is_not_one_role(self):
        client = self.client(.9, .88)
        result = resolve_predicate_columns(
            'Show held and shipped records', 'orders',
            self.columns('stage', 'shipment'), client,
            value_evidence={
                'stage': ('open', 'closed', 'held'),
                'shipment': ('closed', 'shipped', 'returned'),
            })
        self.assertEqual(result.columns, ('stage', 'shipment'))
        self.assertEqual(result.status, 'resolved')
        self.assertEqual(result.uncertain_columns, ())
        self.assertEqual(client.call.call_count, 1)

    def test_confident_choice_can_promote_one_uncertain_column(self):
        client = self._role_choice((.46, .42), 'c0', .86)
        result = resolve_predicate_columns(
            'Count players who bat left-handed.', 'players',
            self.columns('bats', 'throws'), client,
            value_evidence={'bats': ('L', 'R'), 'throws': ('L', 'R')})
        self.assertEqual(result.columns, ('bats',))
        self.assertEqual(result.uncertain_columns, ())

    def test_literal_identity_reaches_the_semantic_role_call(self):
        client = self.client(.99)
        facts = ({'id': 'integer:0:4', 'kind': 'integer', 'value': 1998},)
        resolve_predicate_columns('1998', 'entries', self.columns('year'), client, literal_facts=facts)
        self.assertEqual(client.call.call_args.args[0]['exact_request_operands'], facts)
