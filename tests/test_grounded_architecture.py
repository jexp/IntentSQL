"""Semantic evidence, assignment isolation, and safety boundaries."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from intentsql import mutations, read_engine
from intentsql.capabilities import comparison_choices, validate_operand
from intentsql.database import connect
from intentsql.semantic_read import Condition, ConditionPlan
from intentsql.skills.schema import Column
from intentsql.skills.predicate import resolve_predicate_operator
from intentsql.jev_client import CallResult


class GroundedArchitectureTests(unittest.TestCase):
    def test_registry_limits_model_choices_and_compiler_operands(self):
        self.assertIn('PREFIX', comparison_choices('text'))
        self.assertNotIn('PREFIX', comparison_choices('date'))
        self.assertNotIn('CONTAINS', comparison_choices('numeric'))
        for operator, value in [('=', {}), ('BETWEEN', (1,)), ('IS NULL', 1),
                                ('IN', (None,)), ('DROP TABLE', 'x')]:
            with self.subTest(operator=operator), self.assertRaises(ValueError):
                validate_operand(operator, value)

    def test_other_columns_comparison_words_cannot_override_this_column(self):
        client = Mock()
        client.call.return_value = CallResult({'answers': {'operator': {
            'choice': '=', 'probabilities': {'=': .99}}}}, 20, 3, 1)
        result = resolve_predicate_operator('amount is 5 and age is greater than 30',
            Column('amount', 'INTEGER', True, False), client)
        self.assertEqual(result.operator, '=')
        client.call.assert_called_once()

    def test_lowercase_multiword_insert_operand_is_exact_request_span(self):
        request = 'Add an item with name new test item, stock 12.'
        spans = mutations.literals_from_request(request)
        match = next(item for item in spans if item.value == 'new test item')
        self.assertEqual(request[match.start:match.end], match.value)
        self.assertFalse(any('item,' in str(item.value) for item in spans))

    def test_independent_assignments_share_a_call_with_exact_provenance(self):
        request = 'Add item with name new test item, stock 12.'
        program = mutations.MutationProgram('INSERT', 'items')
        columns = [Column('name', 'TEXT', True, False), Column('stock', 'INTEGER', True, False)]
        def answer(state, questions):
            values = ('new test item', 12)
            return {'answers': {key: {'choice': next(identifier for identifier, item in q['criteria'].items()
                if isinstance(item, dict) and item['value'] == value)}
                for (key,q), value in zip(questions.items(), values)}}
        with patch.object(read_engine, 'system_one', side_effect=answer) as call:
            mutations.bind_assignments(request, program, columns, mutations.literals_from_request(request))
        call.assert_called_once()
        self.assertEqual(program.assignments, {'name': 'new test item', 'stock': 12})
        self.assertEqual(len(program.value_provenance), 2)

    def test_assignment_and_old_row_condition_on_same_column(self):
        request = 'Set amount to 9 for entries whose amount exceeds 5.'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'fixture.db'
            with connect(path) as conn:
                conn.execute('CREATE TABLE entries(id INTEGER PRIMARY KEY, amount INTEGER)')
                conn.execute('INSERT INTO entries VALUES(1,6)')
            def choose(question, schema, program, stage, instruction, candidates):
                return {'choose operation':'UPDATE', 'choose table':'entries',
                        'choose target shape':'predicate'}[stage]
            def answers(state, questions):
                if 'a0' in questions:
                    key = next(k for k,v in questions['a0']['criteria'].items()
                               if isinstance(v,dict) and v['value']==9)
                    return {'answers': {'a0': {'choice': key}}}
                return {'answers': {'c0': {'noul': .99}}}
            def conditions(conn, question, entity, columns, client, facts, *args, **kwargs):
                self.assertNotIn('9', question)
                self.assertIn('5', question)
                self.assertEqual([f.value for f in facts], [5])
                self.assertEqual(client.state.assignments, (('amount',9),))
                self.assertFalse(kwargs.get('excluded_columns'))
                return ConditionPlan((Condition('amount','>',5),), 'AND', ())
            with patch.object(mutations,'select',side_effect=choose), \
                 patch.object(read_engine,'system_one',side_effect=answers), \
                 patch.object(mutations,'plan_conditions',side_effect=conditions), \
                 patch.object(mutations,'vet_mutation',return_value={'passed':True}):
                result=mutations.plan_mutation(path, request)
            self.assertEqual(result['params'],[9,5])
            self.assertEqual(result['after'][0]['amount'],9)
            with connect(path) as conn:
                self.assertEqual(conn.execute('SELECT amount FROM entries').fetchone()[0],6)

    def test_preview_enforces_foreign_keys_and_cascade_row_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fixture.db'
            with connect(path) as conn:
                conn.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
                conn.execute('CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE)')
                conn.execute('INSERT INTO parent VALUES(1)')
                conn.executemany('INSERT INTO child VALUES(?,1)',[(i,) for i in range(101)])
            with connect(path) as conn:
                conn.row_factory=sqlite3.Row
                insert=mutations.MutationProgram('INSERT','child',{'id':200,'parent_id':99})
                with self.assertRaises(sqlite3.IntegrityError):
                    mutations.preview_mutation(conn,insert,'INSERT INTO child VALUES(?,?)',[200,99],[])
                delete=mutations.MutationProgram('DELETE','parent',where_column='id',where_value=1)
                with self.assertRaisesRegex(ValueError,'Unsafe affected-row count'):
                    mutations.preview_mutation(conn,delete,'DELETE FROM parent WHERE id=?',[1],[{'id':1,'_rowid_':1}])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM child').fetchone()[0],101)

    def test_joined_operand_keeps_its_own_table_context(self):
        from intentsql.decision_context import DecisionClient, ReadState, Stage
        transport=Mock()
        client=DecisionClient(transport,ReadState('source'))
        client.advance(Stage.VALUE,predicate_table='related',predicate_column='label',comparison='=')
        client.call({'condition_column':'label','comparison':'='},{'value':{}})
        state=transport.call.call_args.args[0]
        self.assertEqual(state['condition_table'],'related')
        self.assertEqual(state['decision_context']['established']['source'],'source')
        self.assertEqual(state['decision_context']['established']['predicate_table'],'related')

    def test_binding_can_retract_an_assignment_role_nomination(self):
        program=mutations.MutationProgram('UPDATE','items')
        question='Change stock of Alpha to 9'
        columns=[Column('name','TEXT',True,False),Column('stock','INTEGER',True,False)]
        evidence=mutations.literals_from_request(question)
        number=next(item for item in evidence if item.value==9)
        with patch.object(read_engine,'system_one',return_value={'answers': {
                'a0':{'choice':'none'},'a1':{'choice':number.evidence_id}}}):
            mutations.bind_assignments(question,program,columns,evidence)
        self.assertEqual(program.assignments,{'stock':9})
        with patch.object(read_engine,'system_one',return_value={'answers':{'a0':{'choice':'none'}}}), \
             self.assertRaisesRegex(ValueError,'No requested assignment'):
            mutations.bind_assignments('unclear',mutations.MutationProgram('UPDATE','items'),columns[:1],[])
