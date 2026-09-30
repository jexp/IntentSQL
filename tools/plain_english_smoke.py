#!/usr/bin/env python3
"""Targeted live smoke checks for IntentSQL's plain-English architecture.

Run this against a local IntentSQL server. It intentionally tests ordinary,
short phrasing before the larger benchmark suites.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable


def run_sse(base_url: str, database: str, prompt: str, timeout: float) -> dict[str, Any]:
    payload = json.dumps({"database": database, "prompt": prompt, "mode": "read"}).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/run",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    except Exception as exc:
        return {"result": None, "error": f"{type(exc).__name__}: {exc}",
                "events": events, "elapsed_s": time.perf_counter() - started}

    result = None
    error = None
    for event in events:
        if event.get("kind") == "result":
            result = event.get("result")
        elif event.get("kind") == "error":
            error = event.get("message") or "Unknown IntentSQL error"
    return {"result": result, "error": error, "events": events,
            "elapsed_s": time.perf_counter() - started}


def filt(program: dict[str, Any], column: str, operator: str, value: Any) -> bool:
    return any(item.get("column") == column and item.get("operator") == operator and
               item.get("value") == value for item in program.get("filters", []))


def per_group(program: dict[str, Any], *, selection: str | None = None,
              group: str, column: str, function: str) -> bool:
    item = program.get("per_group_extremum") or {}
    return (item.get("group_column") == group and item.get("extremum_column") == column and
            item.get("function") == function and
            (selection is None or item.get("selection") == selection))


def rows_contain(result: dict[str, Any], *values: Any) -> bool:
    target = {str(v) for v in values}
    return any(target.issubset({str(v) for v in row}) for row in result.get("rows", []))


def grouped_school_counts(result: dict[str, Any]) -> bool:
    actual = {tuple(row) for row in result.get("rows", [])}
    return {("Public School", 1761), ("Charter School", 76)}.issubset(actual)


def check_first(result: dict[str, Any]) -> bool:
    return result.get("total_rows") == 14 and per_group(
        result.get("program", {}), selection="per_group_extremum",
        group="season", column="episode_in_season", function="MIN")


def check_last(result: dict[str, Any]) -> bool:
    return result.get("total_rows") == 14 and per_group(
        result.get("program", {}), selection="per_group_extremum",
        group="season", column="episode_in_season", function="MAX")


def check_one(result: dict[str, Any]) -> bool:
    return result.get("total_rows") == 14 and per_group(
        result.get("program", {}), selection="per_group_representative",
        group="season", column="id", function="MIN")


Case = tuple[str, str, str, Callable[[dict[str, Any]], bool]]
CASES: list[Case] = [
    ("PG01", "cyberchase.db", "Return the first episode from each season.", check_first),
    ("PG02", "cyberchase.db", "Show the last episode from every season.", check_last),
    ("PG03", "cyberchase.db", "Show me one episode from each season.", check_one),
    ("S01", "cyberchase.db", "Show episodes from season 2.",
     lambda r: r.get("total_rows") == 14 and filt(r.get("program", {}), "season", "=", 2)),
    ("S02", "cyberchase.db", "Give me five episode titles in ID order.",
     lambda r: r.get("total_rows") == 5 and r.get("program", {}).get("limit") == 5),
    ("G01", "dese.db", "Count schools by type.", grouped_school_counts),
    ("G02", "dese.db", "List each school type and its number of schools, largest count first.",
     lambda r: grouped_school_counts(r) and bool((r.get("program", {}).get("order_by") or []))),
    ("F01", "dese.db", "Show Boston schools.",
     lambda r: r.get("total_rows") == 15 and filt(r.get("program", {}), "city", "=", "Boston")),
    ("J01", "dese.db", "Which district spends the least per pupil?",
     lambda r: rows_contain(r, "Greater Commonwealth Virtual District", 10043.6) and
               (r.get("program", {}).get("limit") in (1, None))),
    ("A01", "moneyball.db", "How many players were born in Canada?",
     lambda r: r.get("rows") == [[199]] or r.get("rows") == [(199,)]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:7862")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--case", action="append", help="Run only one or more case IDs")
    parser.add_argument("--stop-on-fail", action="store_true")
    args = parser.parse_args()

    wanted = set(args.case or [])
    selected = [case for case in CASES if not wanted or case[0] in wanted]
    if wanted - {case[0] for case in selected}:
        print("Unknown case(s):", ", ".join(sorted(wanted - {case[0] for case in selected})))
        return 2

    passed = 0
    for case_id, database, prompt, checker in selected:
        print(f"\n{'=' * 88}\n{case_id} · {database}\n{prompt}")
        run = run_sse(args.base_url, database, prompt, args.timeout)
        result = run["result"]
        if run["error"] or not isinstance(result, dict):
            print(f"FAIL · {run['error'] or 'no result'} · {run['elapsed_s']:.2f}s")
            if args.stop_on_fail:
                return 1
            continue
        ok = False
        try:
            ok = bool(checker(result))
        except Exception as exc:
            print(f"CHECK ERROR: {type(exc).__name__}: {exc}")
        print(("PASS" if ok else "FAIL") + f" · {run['elapsed_s']:.2f}s")
        print("SQL:", result.get("sql"))
        print("PARAMS:", result.get("params"))
        print("PROGRAM:", json.dumps(result.get("program"), indent=2, ensure_ascii=False))
        stats = result.get("stats") or {}
        print("USAGE:", json.dumps({k: stats.get(k) for k in
              ("jev_calls", "input_tokens", "output_tokens", "cost_usd")}, ensure_ascii=False))
        if ok:
            passed += 1
        elif args.stop_on_fail:
            return 1

    print(f"\n{'=' * 88}\n{passed}/{len(selected)} targeted plain-English checks passed")
    return 0 if passed == len(selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
