#!/usr/bin/env python3
"""30 fresh live IntentSQL checks across three unrelated SQLite databases.

This is intentionally *not* the frozen benchmark corpus.  It is a final targeted
architecture shake-out using new surface forms, direct execution equivalence
against reference SQL, a few typed-plan invariants, and expected-rejection cases.

Run from the IntentSQL repository, with the server already running:

    python tools/final_30_prompt_test.py

Or run selected cases:

    python tools/final_30_prompt_test.py --case T06 --case T07 --case T26
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    database: str
    prompt: str
    reference_sql: str | None = None
    ordered: bool = False
    expect_reject: bool = False
    program_check: Callable[[dict[str, Any]], tuple[bool, str]] | None = None


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
        return {
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
            "events": events,
            "elapsed_s": time.perf_counter() - started,
        }

    result = None
    error = None
    for event in events:
        if event.get("kind") == "result":
            result = event.get("result")
        elif event.get("kind") == "error":
            error = event.get("message") or "Unknown IntentSQL error"
    return {
        "result": result,
        "error": error,
        "events": events,
        "elapsed_s": time.perf_counter() - started,
    }


def reference_rows(repo_root: Path, case: Case) -> list[list[Any]]:
    assert case.reference_sql is not None
    path = repo_root / "data" / case.database
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        return [list(row) for row in conn.execute(case.reference_sql).fetchall()]


def norm_value(value: Any) -> tuple[str, Any]:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return ("num", round(number, 8))
    return ("text", str(value))


def norm_row(row: list[Any] | tuple[Any, ...]) -> tuple[tuple[str, Any], ...]:
    # SQL column order is not semantically important for this shake-out.  The
    # test still catches missing/extra values because row cardinality is kept.
    return tuple(sorted((norm_value(value) for value in row), key=repr))


def rows_equivalent(actual: Any, expected: list[list[Any]], ordered: bool) -> bool:
    if not isinstance(actual, list):
        return False
    actual_rows = [norm_row(row) for row in actual if isinstance(row, (list, tuple))]
    expected_rows = [norm_row(row) for row in expected]
    if len(actual_rows) != len(actual):
        return False
    if ordered:
        return actual_rows == expected_rows
    return collections.Counter(actual_rows) == collections.Counter(expected_rows)


def per_group(selection: str, group: str, column: str, function: str):
    def check(program: dict[str, Any]) -> tuple[bool, str]:
        item = program.get("per_group_extremum") or {}
        ok = (
            item.get("selection") == selection
            and item.get("group_column") == group
            and item.get("extremum_column") == column
            and item.get("function") == function
        )
        return ok, f"expected {selection} over {group}/{column}/{function}; got {item}"
    return check


def joined_extremum(direction: str):
    def check(program: dict[str, Any]) -> tuple[bool, str]:
        joined = set(program.get("joined_tables") or ())
        order = program.get("order_by") or []
        global_extremum = program.get("global_extremum") or {}
        expected_function = {"ASC": "MIN", "DESC": "MAX"}[direction]
        ordered_limit = (
            program.get("limit") == 1
            and any(item.get("direction") == direction and
                    "per_pupil_expenditure" in str(item.get("key")) for item in order)
        )
        tie_preserving_extremum = (
            global_extremum.get("column") == "per_pupil_expenditure"
            and global_extremum.get("function") == expected_function
        )
        ok = (
            "districts" in joined
            and "expenditures" in joined
            and (ordered_limit or tie_preserving_extremum)
        )
        return ok, (
            f"expected joined {direction} extremum via LIMIT 1 or typed global extremum; "
            f"joined={joined}, order={order}, limit={program.get('limit')}, "
            f"global_extremum={global_extremum}"
        )
    return check


def grouped_measure(function: str, group: str):
    def check(program: dict[str, Any]) -> tuple[bool, str]:
        groups = program.get("groups") or []
        group_names = [item.get("group by") for item in groups if isinstance(item, dict)]
        outputs = [str(value) for value in (program.get("outputs") or [])]
        ok = group in group_names and any(function in value for value in outputs)
        return ok, f"expected grouped {function} by {group}; groups={groups}, outputs={outputs}"
    return check


CASES: list[Case] = [
    # Cyberchase: short everyday phrasing + per-group shapes.
    Case("T01", "plain", "cyberchase.db",
         "Give me every episode from season 4.",
         'SELECT * FROM episodes WHERE season = 4'),
    Case("T02", "plain", "cyberchase.db",
         "How many episodes were made for season 11?",
         'SELECT COUNT(*) FROM episodes WHERE season = 11'),
    Case("T03", "text", "cyberchase.db",
         "List episode titles that begin with The, A to Z.",
         "SELECT title FROM episodes WHERE title LIKE 'The%' ORDER BY title ASC", ordered=True),
    Case("T04", "ordering", "cyberchase.db",
         "Show the two earliest airings. Return title and air date.",
         'SELECT title, air_date FROM episodes ORDER BY air_date ASC LIMIT 2', ordered=True),
    Case("T05", "distinct", "cyberchase.db",
         "How many unique topics are there?",
         'SELECT COUNT(DISTINCT topic) FROM episodes'),
    Case("T06", "per-group", "cyberchase.db",
         "Return the first episode from every season.",
         'SELECT * FROM episodes AS base WHERE base.episode_in_season = '
         '(SELECT MIN(grouped.episode_in_season) FROM episodes AS grouped '
         'WHERE grouped.season = base.season)',
         program_check=per_group("per_group_extremum", "season", "episode_in_season", "MIN")),
    Case("T07", "per-group", "cyberchase.db",
         "Pick one episode from each season.",
         'SELECT * FROM episodes AS base WHERE base.id = '
         '(SELECT MIN(grouped.id) FROM episodes AS grouped WHERE grouped.season = base.season)',
         program_check=per_group("per_group_representative", "season", "id", "MIN")),
    Case("T08", "grouping", "cyberchase.db",
         "Which seasons contain at least ten episodes? Return season and count, busiest first.",
         'SELECT season, COUNT(*) FROM episodes GROUP BY season HAVING COUNT(*) >= 10 '
         'ORDER BY COUNT(*) DESC',
         program_check=grouped_measure("COUNT", "season")),
    Case("T09", "text", "cyberchase.db",
         "List titles ending in Day alphabetically.",
         "SELECT title FROM episodes WHERE title LIKE '%Day' ORDER BY title ASC", ordered=True),

    # DESE: negative categories, grouping, direct joins, quantities.
    Case("T10", "plain", "dese.db",
         "Show every school located in Cambridge.",
         "SELECT * FROM schools WHERE city = 'Cambridge'"),
    Case("T11", "negation", "dese.db",
         "How many Worcester schools are not charter schools?",
         "SELECT COUNT(*) FROM schools WHERE city = 'Worcester' AND type != 'Charter School'"),
    Case("T12", "filter+order", "dese.db",
         "List Boston charter school names alphabetically.",
         "SELECT name FROM schools WHERE city = 'Boston' AND type = 'Charter School' ORDER BY name ASC",
         ordered=True),
    Case("T13", "group-extremum", "dese.db",
         "Which school type occurs least often? Return type and count.",
         'SELECT type, COUNT(*) FROM schools GROUP BY type ORDER BY COUNT(*) ASC, type ASC LIMIT 1',
         ordered=True, program_check=grouped_measure("COUNT", "type")),
    Case("T14", "group-top-n", "dese.db",
         "Give me the three cities with the most schools. Show city and count.",
         'SELECT city, COUNT(*) FROM schools GROUP BY city ORDER BY COUNT(*) DESC, city ASC LIMIT 3', ordered=True,
         program_check=grouped_measure("COUNT", "city")),
    Case("T15", "join-extremum", "dese.db",
         "Which district spends the most per pupil? Return its name and the amount.",
         'SELECT d.name, e.per_pupil_expenditure FROM expenditures e '
         'JOIN districts d ON d.id = e.district_id '
         'WHERE e.per_pupil_expenditure = (SELECT MAX(per_pupil_expenditure) FROM expenditures) '
         'ORDER BY e.per_pupil_expenditure DESC',
         ordered=True, program_check=joined_extremum("DESC")),
    Case("T16", "join-range", "dese.db",
         "Show districts spending between 32k and 35k per pupil, cheapest first.",
         'SELECT d.name, e.per_pupil_expenditure FROM expenditures e '
         'JOIN districts d ON d.id = e.district_id '
         'WHERE e.per_pupil_expenditure BETWEEN 32000 AND 35000 '
         'ORDER BY e.per_pupil_expenditure ASC', ordered=True),
    Case("T17", "text+limit", "dese.db",
         "Find four schools with Academy in the name. Return school name and city alphabetically.",
         "SELECT name, city FROM schools WHERE name LIKE '%Academy%' ORDER BY name ASC LIMIT 4", ordered=True),
    Case("T18", "two-group", "dese.db",
         "For Boston and Cambridge, show the number of schools for each city and school type.",
         "SELECT city, type, COUNT(*) FROM schools WHERE city IN ('Boston', 'Cambridge') "
         "GROUP BY city, type"),

    # Moneyball: aliases, NULL, worded magnitude, aggregates, generalized representative.
    Case("T19", "categorical", "moneyball.db",
         "How many players were born in Cuba?",
         "SELECT COUNT(*) FROM players WHERE birth_country = 'Cuba'"),
    Case("T20", "alias+secondary-order", "moneyball.db",
         "Give me three Canada-born players with the greatest height, breaking ties by last name. "
         "Return first name, last name, and height.",
         "SELECT first_name, last_name, height FROM players WHERE birth_country = 'CAN' "
         "ORDER BY height DESC, last_name ASC LIMIT 3", ordered=True),
    Case("T21", "null", "moneyball.db",
         "How many players have a batting side recorded?",
         'SELECT COUNT(*) FROM players WHERE bats IS NOT NULL'),
    Case("T22", "worded-number", "moneyball.db",
         "How many salary records from 2001 are above ten million?",
         'SELECT COUNT(*) FROM salaries WHERE year = 2001 AND salary > 10000000'),
    Case("T23", "aggregate-round", "moneyball.db",
         "What was the average salary in 2000, rounded to the nearest whole number?",
         'SELECT ROUND(AVG(salary), 0) FROM salaries WHERE year = 2000'),
    Case("T24", "group-top-n", "moneyball.db",
         "Which five years have the highest single-performance home-run total? Return year and maximum.",
         'SELECT year, MAX(HR) FROM performances GROUP BY year ORDER BY MAX(HR) DESC, year ASC LIMIT 5', ordered=True,
         program_check=grouped_measure("MAX", "year")),
    Case("T25", "having", "moneyball.db",
         "Which years had a maximum home-run total of at least 70? Return year and maximum.",
         'SELECT year, MAX(HR) FROM performances GROUP BY year HAVING MAX(HR) >= 70',
         program_check=grouped_measure("MAX", "year")),
    Case("T26", "per-group", "moneyball.db",
         "Show one player from each batting side.",
         'SELECT * FROM players AS base WHERE base.id = '
         '(SELECT MIN(grouped.id) FROM players AS grouped WHERE grouped.bats = base.bats)',
         program_check=per_group("per_group_representative", "bats", "id", "MIN")),

    # IntentSQL should fail closed on these shapes rather than approximate them.
    Case("T27", "reject", "cyberchase.db",
         "For each season, give me its two earliest episodes.", expect_reject=True),
    Case("T28", "reject", "moneyball.db",
         "What is the average of the maximum home-run total for each year?", expect_reject=True),
    Case("T29", "reject", "dese.db",
         "Show every school together with the per-pupil expenditure of its district.", expect_reject=True),
    Case("T30", "reject", "moneyball.db",
         "Show salary records that are above that player's own average salary.", expect_reject=True),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:7862")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--case", action="append", help="Run only one or more case IDs")
    parser.add_argument("--stop-on-fail", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    if not (repo_root / "data").is_dir():
        parser.error(f"--repo-root does not look like IntentSQL: {repo_root}")

    wanted = set(args.case or [])
    selected = [case for case in CASES if not wanted or case.id in wanted]
    missing = wanted - {case.id for case in selected}
    if missing:
        parser.error("unknown case(s): " + ", ".join(sorted(missing)))

    passed = 0
    category_totals: collections.Counter[str] = collections.Counter()
    category_passed: collections.Counter[str] = collections.Counter()
    total_usage = collections.Counter()

    for case in selected:
        category_totals[case.category] += 1
        print(f"\n{'=' * 100}\n{case.id} · {case.category} · {case.database}\n{case.prompt}")
        run = run_sse(args.base_url, case.database, case.prompt, args.timeout)
        result = run["result"]
        error = run["error"]
        reasons: list[str] = []

        if case.expect_reject:
            rejected = bool(error) or not isinstance(result, dict) or not result.get("supported", True)
            if not rejected:
                reasons.append("request was expected to fail closed but executed")
        else:
            if error:
                reasons.append(f"IntentSQL error: {error}")
            elif not isinstance(result, dict):
                reasons.append("no result event returned")
            elif not result.get("supported", True):
                reasons.append("IntentSQL marked a supported case unsupported")
            else:
                sql = str(result.get("sql") or "")
                if "__intentsql_" in sql:
                    reasons.append("internal __intentsql_* SQL alias leaked into generated SQL")
                expected = reference_rows(repo_root, case)
                if not rows_equivalent(result.get("rows"), expected, case.ordered):
                    reasons.append(
                        f"execution result differs from reference SQL: expected {expected[:8]!r}" +
                        (" ..." if len(expected) > 8 else "")
                    )
                if case.program_check:
                    ok, detail = case.program_check(result.get("program") or {})
                    if not ok:
                        reasons.append("typed-plan invariant failed: " + detail)

        ok = not reasons
        print(("PASS" if ok else "FAIL") + f" · {run['elapsed_s']:.2f}s")
        if error:
            print("ERROR:", error)
        if isinstance(result, dict):
            print("SQL:", result.get("sql"))
            print("PARAMS:", result.get("params"))
            stats = result.get("stats") or {}
            usage = {key: stats.get(key) for key in
                     ("jev_calls", "input_tokens", "output_tokens", "cost_usd")}
            print("USAGE:", json.dumps(usage, ensure_ascii=False))
            for key in ("jev_calls", "input_tokens", "output_tokens"):
                if isinstance(stats.get(key), (int, float)):
                    total_usage[key] += stats[key]
            if isinstance(stats.get("cost_usd"), (int, float)):
                total_usage["cost_usd"] += stats["cost_usd"]
        if reasons:
            for reason in reasons:
                print("  -", reason)
            if isinstance(result, dict):
                print("PROGRAM:", json.dumps(result.get("program"), indent=2, ensure_ascii=False))
        else:
            passed += 1
            category_passed[case.category] += 1

        if reasons and args.stop_on_fail:
            break

    attempted = sum(category_totals.values())
    print(f"\n{'=' * 100}")
    print(f"FINAL: {passed}/{attempted} cases passed")
    for category in sorted(category_totals):
        print(f"  {category:22s} {category_passed[category]}/{category_totals[category]}")
    if attempted:
        print("USAGE TOTALS:", json.dumps(dict(total_usage), ensure_ascii=False))
    return 0 if passed == attempted else 1


if __name__ == "__main__":
    raise SystemExit(main())
