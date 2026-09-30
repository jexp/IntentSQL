"""Staged execution-equivalence benchmark. Run from the repository root."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from intentsql import mutations, read_engine as engine  # noqa: E402


def normalized_rows(rows):
    return [tuple(round(value, 9) if isinstance(value, float) else value for value in row) for row in rows]


def equivalent(actual, expected, ordered=False):
    if len(actual) != len(expected):
        return False
    if not actual:
        return True
    if len(actual[0]) != len(expected[0]):
        return False
    from itertools import permutations
    width = len(actual[0])
    # Column presentation order is irrelevant, but extra/missing outputs are not.
    for positions in permutations(range(width)):
        mapped = [tuple(row[pos] for pos in positions) for row in normalized_rows(actual)]
        gold = normalized_rows(expected)
        if mapped == gold if ordered else Counter(mapped) == Counter(gold):
            return True
    return False


def first_incorrect_decision(case, trace):
    """Report the earliest missing required step; otherwise leave diagnosis open."""
    for stage, fragment in case.get("expected_decisions", []):
        matched = [entry for entry in trace if stage in entry["kind"]]
        if matched and not any(fragment.lower() in entry["selected"].lower() for entry in matched):
            return {"stage": stage, "selected": matched[0]["selected"],
                    "expected_contains": fragment, "basis": "required benchmark decision"}
        if not matched:
            return {"stage": stage, "selected": None, "expected_contains": fragment,
                    "basis": "required decision stage absent"}
    return {"stage": None, "selected": None, "basis": "requires manual trace review"}


def read_case(case, path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        gold = conn.execute(case["gold_sql"]).fetchall()
    expected_rows = case.get("expected_rows")
    if expected_rows is not None and len(gold) != expected_rows:
        raise ValueError(f"Gold SQL returned {len(gold)} rows; independent expected dimension is {expected_rows}")
    result = engine.compile_and_run(str(path), case["question"])
    passed = not result["unsupported"] and equivalent(result["rows"], gold, case.get("ordered", False))
    candidate_equivalent = False
    if result["unsupported"] and result["sql"]:
        # Benchmark-only diagnostic: execute a rejected SELECT in a separate
        # read-only connection to distinguish planner errors from false vetoes.
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                conn.execute("PRAGMA query_only=ON")
                candidate = conn.execute(result["sql"], result["params"]).fetchall()
            candidate_equivalent = equivalent(candidate, gold, case.get("ordered", False))
        except sqlite3.Error:
            pass
    if passed:
        first_error = None
    elif case.get("known_gap") and result["unsupported"]:
        first_error = {"stage": "UNSUPPORTED CAPABILITY", "selected": None,
                       "basis": "compiler could not represent the requested operation; not assigned to a Jev choice"}
    elif candidate_equivalent:
        first_error = {"stage": "FINAL VET", "selected": "rejected execution-equivalent SQL",
                       "basis": "benchmark-only candidate execution"}
    else:
        first_error = first_incorrect_decision(case, engine.TRACE)
    return {"passed": passed, "supported": not result["unsupported"],
            "actual_rows": len(result["rows"]), "gold_rows": len(gold),
            "actual_sample": result["rows"][:5], "gold_sample": gold[:5],
            "program": result["program"].state(),
            "vet": result.get("vet"),
            "sql": result["sql"], "params": result["params"],
            "candidate_equivalent": candidate_equivalent,
            "vet_caught_incorrect": bool(result["unsupported"] and result["sql"] and result.get("vet") and not candidate_equivalent),
            "vet_false_negative": bool(result["unsupported"] and candidate_equivalent),
            "vet_missed_incorrect": bool(not passed and not result["unsupported"]),
            "first_incorrect_decision": first_error}


def change_case(case, path):
    with tempfile.TemporaryDirectory() as folder:
        working = Path(folder) / path.name
        gold_path = Path(folder) / ("gold-" + path.name)
        shutil.copy2(path, working)
        shutil.copy2(path, gold_path)
        plan = mutations.plan_mutation(working, case["question"])
        if not plan["supported"]:
            return {"passed": False, "supported": False, "vet_caught_incorrect": True,
                    "first_incorrect_decision": first_incorrect_decision(case, engine.TRACE)}
        table = engine.qident(plan["program"]["table"])
        with sqlite3.connect(gold_path) as conn:
            conn.execute(case["gold_sql"])
            gold = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        mutations.commit_mutation(working, plan)
        with sqlite3.connect(working) as conn:
            actual = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        passed = actual == gold
        return {"passed": passed, "supported": True, "sql": plan["sql"],
                "params": plan["params"], "vet_caught_incorrect": False,
                "vet_missed_incorrect": not passed,
                "first_incorrect_decision": None if passed else first_incorrect_decision(case, engine.TRACE)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("smoke", "cs50"), default="smoke")
    parser.add_argument("--database", choices=("cyberchase", "dese", "moneyball"))
    parser.add_argument("--paraphrases", action="store_true", help="Include alternate wordings for selected CS50 cases")
    parser.add_argument("--stage", type=int, help="Run just this stage (1–11)")
    parser.add_argument("--through", type=int, help="Run stages 1 through N")
    parser.add_argument("--case", help="Run one case by ID")
    args = parser.parse_args()
    if args.suite == "cs50" and (args.stage is not None or args.through is not None):
        parser.error("--stage/--through apply only to the smoke suite; use --database or --case for CS50")
    if args.suite == "cs50":
        from cs50_cases import CASES
        cases = CASES
        engine.MAX_RESULT_ROWS = max(engine.MAX_RESULT_ROWS, 20000)
    else:
        cases = json.loads((ROOT / "benchmarks/cases.json").read_text())
    selected = [case for case in cases if
                (args.stage is None or case["stage"] == args.stage) and
                (args.through is None or case["stage"] <= args.through) and
                (args.case is None or case["id"] == args.case) and
                (args.database is None or case["database"] == f"{args.database}.db")]
    if args.paraphrases:
        expanded = []
        for case in selected:
            expanded.append(case)
            for index, question in enumerate(case.get("paraphrases", []), start=1):
                variant = {**case, "id": f"{case['id']}-P{index}", "base_id": case["id"],
                           "variant_index": index, "question": question}
                expanded.append(variant)
        selected = expanded
    if not selected:
        parser.error("No cases selected")
    results = []
    for case in selected:
        path = ROOT / "data" / case["database"]
        try:
            outcome = change_case(case, path) if case.get("mode") == "change" else read_case(case, path)
        except Exception as exc:
            outcome = {"passed": False, "error": str(exc),
                       "first_incorrect_decision": first_incorrect_decision(case, engine.TRACE)}
        record = {"id": case["id"], "stage": case.get("stage"), "question": case["question"],
                  "known_gap": case.get("known_gap", False), **outcome,
                  "base_id": case.get("base_id", case["id"]),
                  "variant_index": case.get("variant_index", 0),
                  "zero_schema_configuration": True,
                  "stats": {**engine.stats, "cost_usd": engine.total_cost_usd()},
                  "trace": list(engine.TRACE)}
        results.append(record)
        label = "PASS" if record["passed"] else "FAIL"
        print(f"{label:4} {case.get('stage', '-'):>2} {case['id']:20} "
              f"calls={engine.stats['jev_calls']} vet={engine.stats['vet_calls']} "
              f"cost≈${engine.total_cost_usd():.5f}", flush=True)
        if not record["passed"]:
            print("     ", record.get("error") or record.get("first_incorrect_decision"), flush=True)
    target = ROOT / "benchmarks/results"
    target.mkdir(exist_ok=True)
    path = target / f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=2, default=str))
    families = {}
    for item in results:
        families.setdefault(item["base_id"], []).append(item)
    paraphrase_groups = [group for group in families.values() if len(group) > 1]
    invariant = sum(all(item["passed"] for item in group) for group in paraphrase_groups)
    same_program = sum(
        all(item["passed"] for item in group) and
        len({json.dumps((item.get("program"), item.get("params")), sort_keys=True, default=str)
             for item in group}) == 1
        for group in paraphrase_groups
    )
    print(f"{sum(item['passed'] for item in results)}/{len(results)} correct · "
          f"paraphrase results {invariant}/{len(paraphrase_groups)} invariant · "
          f"programs {same_program}/{len(paraphrase_groups)} identical · details: {path}")
    print("Vet: "
          f"{sum(bool(item.get('vet_caught_incorrect')) for item in results)} incorrect candidates rejected, "
          f"{sum(bool(item.get('vet_false_negative')) for item in results)} execution-equivalent candidates rejected, "
          f"{sum(bool(item.get('vet_missed_incorrect')) for item in results)} incorrect candidates executed. "
          "Schema-specific mappings: 0.")


if __name__ == "__main__":
    main()
