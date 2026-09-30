#!/usr/bin/env python3
"""Fresh Alpha read smoke: independent SQLite rows and typed query shapes.

Uses disposable imported copies of the bundled databases. Provider exchanges
and generated reports stay outside source control.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import shutil
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.artifacts import artifact_root
from benchmarks.v1.run_benchmark import rejection_pass, rows_equivalent, semantic_check
from tools.e2e import http_json, run_sse


def check_shape(expected, program):
    ok, reasons = semantic_check(expected, program)
    for key in ("filters", "having"):
        wanted = expected.get(key + "_contains", [])
        if len(program.get(key, [])) != len(wanted):
            reasons.append(f"unexpected {key} count")
    query = program.get("typed_query") or {}
    for key in ("aggregate_function", "round_places"):
        if key in expected and query.get(key) != expected[key]:
            reasons.append(f"wrong {key}")
    return not reasons, reasons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:7862")
    parser.add_argument("--case", action="append")
    parser.add_argument("--output", type=Path, default=artifact_root() / "alpha-user-smoke.json")
    args = parser.parse_args()
    cases = json.loads((ROOT / "tools/fixtures/alpha_user_cases.json").read_text())["cases"]
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
    records = []
    imported = {}
    artifact_root().mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="alpha-smoke-", dir=artifact_root())
    try:
        for case in cases:
            name = case["database"]
            source = ROOT / "data" / name
            if name not in imported:
                copied = Path(temporary.name) / (Path(temporary.name).name + "-" + name)
                shutil.copyfile(source, copied)
                imported[name] = http_json(args.base_url, "/api/databases", method="POST",
                                          body={"path": str(copied)})["id"]
            gold = []
            if not case["reject"]:
                with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as conn:
                    gold = [list(row) for row in conn.execute(case["reference_sql"])]
            run = run_sse(args.base_url, imported[name], case["prompt"], "read", 180)
            result = run.get("result") or {}
            rejected = not result.get("supported")
            if case["reject"]:
                passed, reason = rejection_pass(run)
                execution, semantic, reasons = False, False, [reason]
            else:
                execution = not rejected and rows_equivalent(result.get("rows", []), gold, case["ordered"])
                semantic, reasons = check_shape(case["expected_shape"], result.get("program") or {})
                semantic = not rejected and semantic
                passed = execution and semantic
            record = {**case, "passed": passed, "rejected": rejected,
                      "execution_pass": execution, "semantic_pass": semantic,
                      "reasons": reasons, "error": run.get("error"),
                      "database_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                      "reference_row_count": len(gold), "sql": result.get("sql"),
                      "params": result.get("params"), "program": result.get("program"),
                      "stats": result.get("stats") or run.get("stats"),
                      "elapsed_s": run["elapsed_s"], "events": run["events"]}
            records.append(record)
            print(case["id"], "PASS" if passed else "REJECT" if rejected else "FAIL",
                  "rows=" + str(execution), "shape=" + str(semantic),
                  run.get("error") or str(reasons), flush=True)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(records, indent=2, default=str) + "\n")
    finally:
        for database in imported.values():
            http_json(args.base_url, "/api/databases/" + database, method="DELETE", body={"confirm": True})
        temporary.cleanup()
    print(f"{sum(r['passed'] for r in records)}/{len(records)} passed")
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
