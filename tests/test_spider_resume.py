import unittest
import json
from pathlib import Path

from unittest.mock import patch

from benchmarks.spider.run import semantic_check, validate_resume
from benchmarks.spider.run import architecture_digest
from benchmarks.artifacts import artifact_root


class SpiderResumeTests(unittest.TestCase):
    def test_generated_artifacts_default_outside_repository(self):
        repository = Path(__file__).resolve().parents[1]
        self.assertFalse(artifact_root().is_relative_to(repository))

    def test_historical_targeted_plan_keeps_its_frozen_architecture(self):
        path = Path(__file__).resolve().parents[1] / "benchmarks/semantic-final-plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(plan["frozen_before_execution"])
        self.assertEqual(len(plan["targeted_cases"]), 10)
        # This executed development plan records the compiler used then.
        # A later architecture pass must not rewrite its historical digest.
        self.assertEqual(len(plan["architecture_sha256"]), 64)
        int(plan["architecture_sha256"], 16)

    def test_architecture_hash_is_independent_of_checkout_line_endings(self):
        from benchmarks.spider import run
        source = run.ROOT / "intentsql" / "database.py"
        original = Path.read_bytes
        lf = b"line one\nline two\n"
        with patch.object(Path, "read_bytes", autospec=True,
                          side_effect=lambda path: (lf.replace(b"\n", b"\r\n")
                                                    if path == source else original(path))):
            crlf_digest = architecture_digest()
        with patch.object(Path, "read_bytes", autospec=True,
                          side_effect=lambda path: lf if path == source else original(path)):
            lf_digest = architecture_digest()
        self.assertEqual(crlf_digest, lf_digest)

    def test_wildcard_is_a_typed_shape_not_a_display_label(self):
        gold = {"table": "items", "output": "*", "limit": None,
                "distinct": False, "filter": None, "order": None}
        program = {"base_table": "items", "joined_tables": ["items"],
                   "outputs": ["all columns"], "typed_query": {"output": "rows"}}
        with patch("benchmarks.spider.run.expected_semantics", return_value=gold):
            self.assertTrue(semantic_check({}, {}, program)[0])
            self.assertFalse(semantic_check({}, {}, {**program, "typed_query": {"output": "fields"}})[0])
            self.assertFalse(semantic_check({}, {}, {**program, "groups": ["name"]})[0])

    def test_only_completed_prefix_on_same_architecture_can_resume(self):
        cases = [{"id": "a"}, {"id": "b"}]
        identity = {"architecture_sha256": "frozen", "model": "same"}
        saved = {**identity, "records": [{"id": "a"}], "in_flight": None}
        validate_resume(saved, identity, cases)
        for changes in ({"architecture_sha256": "changed"}, {"model": "other"},
                        {"in_flight": "b"}, {"records": [{"id": "b"}]},
                        {"records": [{"id": "a"}, {"id": "a"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_resume({**saved, **changes}, identity, cases)
