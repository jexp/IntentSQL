"""Regression checks for transport, events, local safety and Windows lifetimes."""
import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi.testclient import TestClient
from intentsql import mutations, web
from intentsql.database import connect, readonly_uri
from intentsql.jev_client import JevClient
from intentsql.semantic_read import run_read


class ReleaseTests(unittest.TestCase):
    def test_builtin_examples_reference_bundled_databases_and_unique_prompts(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'static/index.html').read_text(encoding='utf-8')
        samples = re.findall(r'class="sample" data-db="([^"]+)" data-prompt="([^"]+)"', html)
        self.assertEqual(len(samples), 5)
        self.assertEqual(len({prompt for _, prompt in samples}), len(samples))
        for database, prompt in samples:
            self.assertTrue((root / 'data' / database).is_file(), database)
            self.assertTrue(prompt.strip())
        self.assertIn((
            'moneyball.db',
            'For performances from 1998 through 2000, return year and total home runs, grouped by year, highest total first.'), samples)

    def test_database_context_commits_closes_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'space # database.db'
            with connect(path) as conn:
                conn.execute('CREATE TABLE items (id INTEGER)')
                conn.execute('INSERT INTO items VALUES (1)')
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')
            with self.assertRaises(ValueError):
                with connect(path) as conn:
                    conn.execute('INSERT INTO items VALUES (2)')
                    raise ValueError('rollback')
            with connect(readonly_uri(path), uri=True) as conn:
                self.assertEqual(conn.execute('SELECT * FROM items').fetchall(), [(1,)])
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute('DELETE FROM items')
            path.unlink()  # Fails on Windows if any connection remains open.

    def test_transport_events_are_ordered_and_never_include_credentials(self):
        events = []
        client = JevClient(api_key='private-test-secret', on_event=events.append)
        payload = {'answers': {'choice': {'choice':'a'}}, 'usage': {'input_tokens':12,'output_tokens':1}}
        with patch('intentsql.jev_client.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            client.call({'request':'test'}, {'choice': {'type':'choice','criteria':{'a':'A'}}})
        self.assertEqual([e['kind'] for e in events], ['call_start','call_end'])
        self.assertEqual(events[0]['stats']['input_tokens'], 0)
        self.assertEqual(events[1]['stats']['input_tokens'], 12)
        self.assertEqual(events[1]['call'], 1)
        self.assertNotIn('private-test-secret', json.dumps(events))

    def test_timeout_and_invalid_json_are_failed_attempts(self):
        for response in (TimeoutError(), io.BytesIO(b'not json')):
            events = []
            client = JevClient(api_key='', on_event=events.append)
            kwargs = {'side_effect':response} if isinstance(response, Exception) else {'return_value':response}
            with patch('intentsql.jev_client.urlopen', **kwargs), self.assertRaises(RuntimeError):
                client.call({}, {'x':{'type':'noul'}})
            self.assertEqual(client.failed_calls, 1)
            self.assertFalse(client.usage_stats()['usage_complete'])
            self.assertEqual(events[-1]['kind'], 'call_error')

    def test_provider_error_body_cannot_echo_secrets(self):
        events = []
        client = JevClient(api_key='private-test-secret', on_event=events.append)
        failure = HTTPError(client.url, 401, 'unauthorized', {}, io.BytesIO(b'private-test-secret'))
        with patch('intentsql.jev_client.urlopen', side_effect=failure), self.assertRaises(RuntimeError) as raised:
            client.call({}, {'x': {'type':'noul'}})
        self.assertNotIn('private-test-secret', str(raised.exception) + json.dumps(events))

    def test_compilation_event_precedes_execution_and_contains_typed_query(self):
        answers = {name: {'noul':0} for name in ('filters','ordering','distinct','grouping','relationship','having','expression','windowing')}
        answers.update(output={'choice':'rows'}, quantity={'choice':'numeric'})
        payload = {'answers':answers,'usage':{'input_tokens':10,'output_tokens':2}}
        events = []
        client = JevClient(api_key='', on_event=events.append)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.db'
            with connect(path) as conn:
                conn.execute('CREATE TABLE items (id INTEGER)')
                conn.executemany('INSERT INTO items VALUES (?)', [(1,), (2,), (3,)])
            coverage_payload = {'answers': {'coverage': {'choice': 'complete'}},
                                'usage': {'input_tokens': 8, 'output_tokens': 1}}
            with patch('intentsql.jev_client.urlopen', side_effect=[
                    io.BytesIO(json.dumps(payload).encode()),
                    io.BytesIO(json.dumps(coverage_payload).encode())]):
                result = run_read(path, 'show the first 2 rows', client, on_event=events.append)
            kinds = [event['kind'] for event in events]
            self.assertLess(kinds.index('program'), kinds.index('compiled'))
            compiled = next(event for event in events if event['kind']=='compiled')
            self.assertEqual(compiled['sql'], 'SELECT * FROM "items" LIMIT ?')
            self.assertEqual(compiled['params'], [2])
            self.assertEqual(compiled['program']['typed_query']['limit'], 2)
            self.assertEqual(result['rows'], [(1,), (2,)])
            self.assertEqual([e['phase'] for e in events if e['kind']=='phase'], ['inspect','resolve','execute'])

    def test_local_api_rejects_cross_origin_writes_and_untrusted_hosts(self):
        with TestClient(web.app, base_url='http://127.0.0.1') as client:
            self.assertEqual(client.post('/api/connection',json={},headers={'Origin':'https://unrelated.example'}).status_code,403)
            self.assertEqual(client.get('/api/health',headers={'Host':'unrelated.example'}).status_code,400)
            response = client.get('/')
            self.assertEqual(response.status_code,200)
            self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])

    def test_local_looking_endpoint_cannot_redirect_a_key_to_remote_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web, 'WORKSPACE', root), patch.object(web, 'CONFIG_FILE', root/'connection.json'):
                with TestClient(web.app, base_url='http://127.0.0.1') as client:
                    for url in ('http://localhost:7861@outside.example/v1/systemone',
                                'http://127.0.0.1:7861@outside.example/v1/systemone'):
                        response = client.post('/api/connection', json={
                            'provider':'custom', 'url':url, 'api_key':'secret'})
                        self.assertEqual(response.status_code, 400)
                    self.assertFalse((root/'connection.json').exists())

    def test_stream_errors_account_for_this_run_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(web,'WORKSPACE',root), patch.object(web,'DB_DIR',root/'databases'), patch.object(web,'BACKUP_DIR',root/'backups'), patch.object(web,'CONFIG_FILE',root/'connection.json'), patch.dict('os.environ',{'SYSTEM_ONE_API_KEY':''}):
                web.engine.stats['input_tokens'] = 999
                with TestClient(web.app, base_url='http://127.0.0.1') as client:
                    response = client.post('/api/run',json={'database':'cyberchase.db','prompt':'show first 2 rows'})
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                self.assertEqual(events[-1]['kind'],'error')
                self.assertEqual(events[-1]['stats']['input_tokens'],0)
                self.assertEqual(events[-1]['stats']['jev_calls'],0)

    def test_wal_change_invalidates_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.db'
            with connect(path) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('CREATE TABLE items (id INTEGER, name TEXT)')
                conn.execute("INSERT INTO items VALUES (1, 'old')")
                conn.commit()
                before = mutations.fingerprint(path)
                conn.execute("UPDATE items SET name='new'")
                conn.commit()
                self.assertNotEqual(before, mutations.fingerprint(path))
                plan = {'supported':True,'vet':{'passed':True},'fingerprint':before,'sql':'DELETE FROM items WHERE id=?','params':[1],'affected':1}
                with self.assertRaisesRegex(ValueError,'changed since preview'):
                    mutations.commit_mutation(path,plan)

    def test_invalid_connection_file_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'connection.json'
            path.write_text('not json')
            with patch.object(web,'CONFIG_FILE',path), TestClient(web.app,base_url='http://127.0.0.1') as client:
                response = client.get('/api/connection')
            self.assertEqual(response.status_code,500)
            self.assertIn('connection.json',response.json()['detail'])
