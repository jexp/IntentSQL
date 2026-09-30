#!/usr/bin/env python3
"""Run the frozen v2 corpus with the shared v1 transport and result evaluator."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.artifacts import artifact_root  # noqa: E402
from benchmarks.v1 import run_benchmark as base  # noqa: E402

HERE = Path(__file__).resolve().parent
V1 = ROOT / "benchmarks/v1/cases.json"
CORPUS = HERE / "cases.json"

_original_lint = base.supported_sql_lint
_original_semantic_check = base.semantic_check
_original_validate = base.validate_corpus


def supported_sql_lint(case: dict) -> list[str]:
    """v2 permits independent conditions on the two sides of one direct join."""
    obsolete = "current direct-join path intentionally benchmarks at most one source predicate"
    return [item for item in _original_lint(case) if item != obsolete]


def semantic_check(expected: dict, program: dict | None) -> tuple[bool, list[str]]:
    """Accept either a one-row minimum or a typed tie-preserving minimum."""
    alternatives = expected.get("any_of")
    if alternatives is None:
        return _original_semantic_check(expected, program)
    failures = []
    for option in alternatives:
        standard = {key: value for key, value in option.items() if key != "global_extremum"}
        passed, reasons = _original_semantic_check(standard, program)
        required_extremum = option.get("global_extremum")
        if required_extremum is not None and not base.subset_dict(
                required_extremum, (program or {}).get("global_extremum") or {}):
            passed = False
            reasons.append("typed global extremum differs")
        if passed:
            return True, []
        failures.append("; ".join(reasons))
    return False, ["no valid semantic alternative: " + " | ".join(failures)]


def validate_corpus(path: Path, *, verbose: bool = True) -> dict:
    manifest = _original_validate(path, verbose=verbose)
    historical = json.loads(V1.read_text(encoding="utf-8"))
    # All v1 questions and independent reference SQL remain in v2. Only the
    # evaluation envelope changes; cases have not been replaced for a score.
    identity = lambda item: (item["id"], item["database"], item["prompt"], item["reference_sql"])
    if [identity(case) for case in manifest["cases"]] != [
            identity(case) for case in historical["cases"]]:
        raise ValueError("v2 must preserve every v1 prompt and reference query")
    return manifest


base.supported_sql_lint = supported_sql_lint
base.semantic_check = semantic_check
base.validate_corpus = validate_corpus
base.DEFAULT_CORPUS = CORPUS
base.RESULTS_DIR = artifact_root() / "v2/results"
base.BENCH_DB_PREFIX = "intentbench_v2_"


def main() -> int:
    before = set(base.RESULTS_DIR.glob("benchmark-v1-*")) if base.RESULTS_DIR.exists() else set()
    result = base.main()
    for path in set(base.RESULTS_DIR.glob("benchmark-v1-*")) - before:
        path.rename(path.with_name(path.name.replace("benchmark-v1-", "benchmark-v2-", 1)))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
