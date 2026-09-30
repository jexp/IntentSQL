#!/usr/bin/env python3
"""Compare active read-graph results with SQLite gold answers."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.cs50_cases import ALPHA_APPLICABLE_IDS, CASES  # noqa: E402
from intentsql.jev_client import JevClient  # noqa: E402
from intentsql.semantic_read import run_read  # noqa: E402
from intentsql.web import connection_settings  # noqa: E402


def canonical(rows, ordered):
    normalized = [tuple(row) for row in rows]
    return normalized if ordered else sorted(normalized, key=lambda row: json.dumps(row, default=str))


def equivalent_output(result, gold_rows, gold_columns, ordered, paraphrase):
    """Paraphrases may ask for more/fewer fields; compare shared row identity then."""
    if not paraphrase:
        return canonical(result["rows"], ordered) == canonical(gold_rows, ordered), "full"
    shared = [name for name in gold_columns if name in result["columns"]]
    if not shared or len(result["rows"]) != len(gold_rows):
        return False, "no_shared_columns"
    actual_indices = [result["columns"].index(name) for name in shared]
    gold_indices = [gold_columns.index(name) for name in shared]
    actual = [tuple(row[index] for index in actual_indices) for row in result["rows"]]
    expected = [tuple(row[index] for index in gold_indices) for row in gold_rows]
    return canonical(actual, ordered) == canonical(expected, ordered), "shared_columns"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*", help="CS50 case IDs, such as C3 C7 C12")
    parser.add_argument("--alpha-applicable", action="store_true",
                        help="run the frozen CS50 subset inside the current alpha capability envelope")
    parser.add_argument("--fixture", type=Path, help="JSON file with schema-independent benchmark prompts and SQLite gold SQL")
    parser.add_argument("--paraphrases", action="store_true")
    args = parser.parse_args()
    cases = json.loads(args.fixture.read_text())["cases"] if args.fixture else CASES
    requested = list(ALPHA_APPLICABLE_IDS) if args.alpha_applicable else args.cases
    if not requested:
        parser.error("Provide case IDs or --alpha-applicable")
    chosen = [case for case in cases if case["id"] in requested]
    if len(chosen) != len(set(requested)):
        parser.error("Unknown or duplicate case ID")
    config = connection_settings()
    client = JevClient(config["url"], config["model"], config["api_key"])
    passed = attempted = 0
    for case in chosen:
        path = ROOT / "data" / case["database"]
        with sqlite3.connect(path) as conn:
            cursor = conn.execute(case["gold_sql"])
            gold = cursor.fetchall()
            gold_columns = [item[0] for item in cursor.description]
        prompts = [case["question"]]
        if args.paraphrases:
            prompts += case.get("paraphrases", [])
        for index, prompt in enumerate(prompts):
            attempted += 1
            steps = []
            try:
                result = run_read(path, prompt, client, on_step=steps.append)
                equivalent, comparison = equivalent_output(
                    result, gold, gold_columns, case.get("ordered", False), index > 0)
                # A short result can coincide with the gold answer even when
                # the model chose an over-broad threshold. Fixtures may assert
                # critical source literals to catch such false positives.
                if equivalent and case.get("required_params"):
                    equivalent = all(value in result["params"]
                                     for value in case["required_params"])
                    if not equivalent:
                        comparison = "missing_required_semantic_operand"
                passed += equivalent
                report = {"case": case["id"], "pass": equivalent,
                                  "rows": result["total_rows"], "gold_rows": len(gold),
                                  "sql": result["sql"], "params": result["params"],
                                  "comparison": comparison,
                                  "calls": result["stats"]["jev_calls"],
                                  "input_tokens": result["stats"]["input_tokens"],
                                  "output_tokens": result["stats"]["output_tokens"],
                                  "prompt": prompt}
                if not equivalent:
                    report["actual_columns"] = result["columns"]
                    report["gold_columns"] = gold_columns
                    report["trace"] = [{"skill": step["name"], "selected": step["selected"]}
                                       for step in steps]
                print(json.dumps(report, default=str))
            except Exception as exc:
                print(json.dumps({"case": case["id"], "pass": False,
                                  "error": str(exc), "prompt": prompt,
                                  "trace": [{"skill": step["name"], "selected": step["selected"]}
                                            for step in steps]}, default=str))
    print(json.dumps({"passed": passed, "attempted": attempted}))
    if passed != attempted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
