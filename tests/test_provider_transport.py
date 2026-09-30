"""Provider transport sends only configured model/auth, without live network calls."""

import io
import json
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from intentsql.jev_client import JevClient
from intentsql import read_engine


RESPONSE = json.dumps({"answers": {"x": {"noul": .7}},
                       "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


class ProviderTransportTests(unittest.TestCase):
    def test_local_request_omits_model_and_auth_even_with_jev_env_key(self):
        with patch.dict(os.environ, {"SYSTEM_ONE_API_KEY": "env-jev-only"}), \
             patch("intentsql.jev_client.urlopen", return_value=io.BytesIO(RESPONSE)) as opened:
            JevClient(url="http://127.0.0.1:7861/v1/systemone", model="", api_key="").call(
                {"request": "example"}, {"x": {"type": "noul", "instructions": "test"}})
        request = opened.call_args.args[0]
        self.assertNotIn("model", json.loads(request.data))
        self.assertIsNone(request.get_header("Authorization"))

    def test_hosted_request_includes_its_own_model_and_key(self):
        with patch("intentsql.jev_client.urlopen", return_value=io.BytesIO(RESPONSE)) as opened:
            JevClient(url="https://example.test/v1/systemone", model="openjev-latest",
                      api_key="openjev-test-key").call({}, {"x": {"type": "noul"}})
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data)["model"], "openjev-latest")
        self.assertEqual(request.get_header("Authorization"), "Bearer openjev-test-key")


    def test_client_events_expose_exact_body_without_auth_secret(self):
        events = []
        state = {"request": "example"}
        questions = {"pick": {"type": "choice", "instructions": "Pick one",
                              "criteria": {"a": "A", "b": "B"}}}
        with patch("intentsql.jev_client.urlopen", return_value=io.BytesIO(RESPONSE)):
            JevClient(url="https://example.test/v1/systemone", model="jev-latest",
                      api_key="do-not-expose", on_event=events.append).call(state, questions)
        started = next(event for event in events if event["kind"] == "call_start")
        ended = next(event for event in events if event["kind"] == "call_end")
        self.assertEqual(started["request_payload"], {
            "state": state, "questions": questions, "model": "jev-latest"})
        self.assertEqual(ended["response_payload"], json.loads(RESPONSE))
        self.assertNotIn("do-not-expose", json.dumps(events))

    def test_mutation_transport_events_expose_exact_body_and_response(self):
        events = []
        state = {"request": "example mutation"}
        questions = {"check": {"type": "noul", "instructions": "Is this correct?"}}
        read_engine.reset_run_state()
        prior_sink = read_engine.EVENT_SINK
        read_engine.EVENT_SINK = events.append
        try:
            with patch.object(read_engine, "SYSTEM_ONE_URL", "https://example.test"), \
                 patch.object(read_engine, "SYSTEM_ONE_MODEL", "jev-latest"), \
                 patch.object(read_engine, "SYSTEM_ONE_API_KEY", "do-not-expose"), \
                 patch("intentsql.read_engine.urlopen", return_value=io.BytesIO(RESPONSE)):
                read_engine.system_one(state, questions)
        finally:
            read_engine.EVENT_SINK = prior_sink
        started = next(event for event in events if event["kind"] == "call_start")
        ended = next(event for event in events if event["kind"] == "call_end")
        self.assertEqual(started["request_payload"], {
            "state": state, "questions": questions, "model": "jev-latest"})
        self.assertEqual(ended["response_payload"], json.loads(RESPONSE))
        self.assertNotIn("do-not-expose", json.dumps(events))

    def test_mutation_transport_can_use_local_no_auth_profile(self):
        with patch.object(read_engine, "SYSTEM_ONE_MODEL", ""), \
             patch.object(read_engine, "SYSTEM_ONE_API_KEY", ""), \
             patch.object(read_engine, "SYSTEM_ONE_URL", "http://127.0.0.1:7861/v1/systemone"), \
             patch("intentsql.read_engine.urlopen", return_value=io.BytesIO(RESPONSE)) as opened:
            read_engine.system_one({}, {"x": {"type": "noul"}})
        request = opened.call_args.args[0]
        self.assertNotIn("model", json.loads(request.data))
        self.assertIsNone(request.get_header("Authorization"))

    def test_client_keeps_failed_call_count_and_reported_usage(self):
        failure = HTTPError("https://example.test", 429, "limited", {},
                            io.BytesIO(json.dumps({"detail": "limited", "usage": {
                                "input_tokens": 17, "output_tokens": 2}}).encode()))
        client = JevClient(url="https://example.test", model="test", api_key="key", max_retries=0)
        with patch("intentsql.jev_client.urlopen", side_effect=failure):
            with self.assertRaises(RuntimeError):
                client.call({}, {"x": {"type": "noul", "instructions": "test"}})
        self.assertEqual(client.usage_stats(), {
            "jev_calls": 1, "input_tokens": 17, "output_tokens": 2,
            "failed_calls": 1, "usage_complete": True})

    def test_client_marks_failed_usage_unknown_when_transport_has_none(self):
        failure = HTTPError("https://example.test", 500, "failed", {}, io.BytesIO(b"oops"))
        client = JevClient(url="https://example.test", model="test", api_key="key", max_retries=0)
        with patch("intentsql.jev_client.urlopen", side_effect=failure):
            with self.assertRaises(RuntimeError):
                client.call({}, {"x": {"type": "noul", "instructions": "test"}})
        stats = client.usage_stats()
        self.assertEqual(stats["jev_calls"], 1)
        self.assertEqual(stats["failed_calls"], 1)
        self.assertFalse(stats["usage_complete"])

    def test_client_retries_transient_http_once(self):
        failure = HTTPError("https://example.test", 520, "overloaded", {}, io.BytesIO(b"oops"))
        success = io.BytesIO(RESPONSE)
        client = JevClient(url="https://example.test", model="test", api_key="key", max_retries=1)
        with patch("intentsql.jev_client.urlopen", side_effect=[failure, success]), \
             patch("intentsql.jev_client.time.sleep"):
            result = client.call({}, {"x": {"type": "noul", "instructions": "test"}})
        self.assertEqual(result.answers["x"]["noul"], .7)
        self.assertEqual(client.usage_stats()["jev_calls"], 2)
        self.assertEqual(client.usage_stats()["failed_calls"], 1)

    def test_mutation_transport_retries_transient_http_once(self):
        failure = HTTPError("https://example.test", 520, "overloaded", {}, io.BytesIO(b"oops"))
        read_engine.reset_run_state()
        with patch.dict(os.environ, {"SYSTEM_ONE_RETRIES": "1"}), \
             patch.object(read_engine, "SYSTEM_ONE_URL", "https://example.test"), \
             patch.object(read_engine, "SYSTEM_ONE_MODEL", "jev-latest"), \
             patch("intentsql.read_engine.urlopen", side_effect=[failure, io.BytesIO(RESPONSE)]), \
             patch("intentsql.read_engine.time.sleep"):
            result = read_engine.system_one({}, {"x": {"type": "noul"}})
        self.assertEqual(result["answers"]["x"]["noul"], .7)
        self.assertEqual(read_engine.usage_stats()["jev_calls"], 2)
        self.assertEqual(read_engine.usage_stats()["failed_calls"], 1)

    def test_mutation_transport_counts_failed_attempt_and_reported_usage(self):
        failure = HTTPError("https://example.test", 422, "invalid", {},
                            io.BytesIO(json.dumps({"detail": "invalid", "usage": {
                                "input_tokens": 23, "output_tokens": 0}}).encode()))
        read_engine.reset_run_state()
        with patch.object(read_engine, "SYSTEM_ONE_URL", "https://example.test"), \
             patch.object(read_engine, "SYSTEM_ONE_MODEL", "jev-latest"), \
             patch("intentsql.read_engine.urlopen", side_effect=failure):
            with self.assertRaises(RuntimeError):
                read_engine.system_one({}, {"x": {"type": "noul", "instructions": "test"}})
        stats = read_engine.usage_stats()
        self.assertEqual(stats["jev_calls"], 1)
        self.assertEqual(stats["input_tokens"], 23)
        self.assertEqual(stats["failed_calls"], 1)
        self.assertTrue(stats["usage_complete"])
