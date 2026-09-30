"""Public release evidence matches its frozen case definitions and summary."""

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text_file_hashes(path):
    """Historical reports hashed CRLF files; Git checkouts can use LF.

    Accept only the two newline encodings of the same bytes. A changed case,
    question or manifest still fails. Do not rewrite historical report hashes.
    """
    lf = path.read_bytes().replace(b"\r\n", b"\n")
    return {hashlib.sha256(lf).hexdigest(),
            hashlib.sha256(lf.replace(b"\n", b"\r\n")).hexdigest()}


class ReleaseResultTests(unittest.TestCase):
    def test_internal_alpha_result_covers_the_frozen_v2_corpus(self):
        corpus_path = ROOT / "benchmarks/v2/cases.json"
        cases = json.loads(corpus_path.read_text(encoding="utf-8"))["cases"]
        result = json.loads((ROOT / "benchmarks/v2/result-alpha.json").read_text(encoding="utf-8"))
        self.assertIn(result["corpus_sha256"], text_file_hashes(corpus_path))
        self.assertEqual([row["id"] for row in result["cases"]], [case["id"] for case in cases])
        self.assertEqual(len(result["cases"]), 80)
        self.assertEqual(sum(row["full_pass"] for row in result["cases"]), 80)
        self.assertTrue(all(not row["unsafe_execution"] for row in result["cases"]))

    def test_spider_result_covers_the_precommitted_manifest(self):
        manifest_path = ROOT / "benchmarks/spider/manifest-7.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = json.loads((ROOT / "benchmarks/spider/result-7.json").read_text(encoding="utf-8"))
        rows = result["cases"]
        self.assertIn(result["manifest_sha256"], text_file_hashes(manifest_path))
        self.assertEqual([row["id"] for row in rows], [case["id"] for case in manifest["cases"]])
        self.assertEqual(sum(row["passed"] for row in rows), result["summary"]["passed"])
        self.assertEqual(sum(row["rejected"] for row in rows), result["summary"]["safe_rejections"])
        self.assertEqual(sum(not row["rejected"] and not row["execution_pass"] for row in rows),
                         result["summary"]["incorrect_executions"])
        self.assertNotIn('"events"', json.dumps(result))


if __name__ == "__main__":
    unittest.main()
