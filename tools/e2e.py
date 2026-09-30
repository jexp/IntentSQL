#!/usr/bin/env python3
"""End-to-end IntentSQL feature regression runner.

Runs requests through the real /api/run SSE endpoint, records the exact SQL,
bound parameters, returned rows, System One trace events and token/cost stats,
and checks the result against known expectations.

The script does NOT commit mutations by default. Mutation tests exercise the
real preview/vet path only. Use --commit-undo to additionally commit one safe
insert and immediately undo it after validating the preview.

Python 3.9+; standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# HTTP / SSE
# ---------------------------------------------------------------------------


def http_json(base_url: str, path: str, *, method: str = "GET", body: Any = None,
              timeout: float = 60.0) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url.rstrip("/") + path,
                                     data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except Exception:
            detail = raw
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def run_sse(base_url: str, database: str, prompt: str, mode: str,
            timeout: float) -> dict[str, Any]:
    payload = json.dumps({"database": database, "prompt": prompt, "mode": mode}).encode("utf-8")
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
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    event = {"kind": "malformed_event", "raw": line[6:]}
                events.append(event)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "events": events,
            "elapsed_s": time.perf_counter() - started,
            "http_error": f"HTTP {exc.code}: {body}",
            "result": None,
            "error": body,
            "stats": None,
        }
    except Exception as exc:
        return {
            "events": events,
            "elapsed_s": time.perf_counter() - started,
            "http_error": None,
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
            "stats": None,
        }

    result = None
    error = None
    stats = None
    for event in events:
        if event.get("kind") == "result":
            result = event.get("result")
            if isinstance(result, dict):
                stats = result.get("stats") or stats
        elif event.get("kind") == "error":
            error = event.get("message") or "Unknown IntentSQL error"
            stats = event.get("stats") or stats

    return {
        "events": events,
        "elapsed_s": time.perf_counter() - started,
        "http_error": None,
        "result": result,
        "error": error,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# SQL display helpers
# ---------------------------------------------------------------------------


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def bind_sql_for_display(sql: str | None, params: Iterable[Any] | None) -> str:
    """Substitute ? parameters for DISPLAY ONLY; never execute this string."""
    if not sql:
        return ""
    values = iter(list(params or []))
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                # SQL escapes quotes by doubling them.
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            try:
                out.append(sql_literal(next(values)))
            except StopIteration:
                out.append("?")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Expectations / tests
# ---------------------------------------------------------------------------


@dataclass
class Case:
    id: str
    feature: str
    database: str
    expected_table: str | None
    prompt: str
    mode: str = "read"
    expected_rows: list[list[Any]] | None = None
    expected_scalar: Any = None
    expected_count: int | None = None
    expected_joined_tables: list[str] | None = None
    expected_affected: int | None = None
    expected_after_contains: dict[str, Any] | None = None
    expect_rejected: bool = False
    sql_contains: list[str] = field(default_factory=list)
    sql_not_contains: list[str] = field(default_factory=list)
    params_contains: list[Any] = field(default_factory=list)
    notes: str = ""


CYBERCHASE_DOZEN = [
    ["Lost My Marbles"], ["Castleblanca"], ["R-Fair City"],
    ["Snow Day to be Exact"], ["Sensible Flats"], ["Zeus on the Loose"],
    ["The Poddleville Case"], ["And They Counted Happily Ever After"],
    ["Clock Like An Egyptian"], ["Secrets of Symmetria"],
    ["A Day at the Spa"], ["Of All The Luck"],
]

HACKER_TITLES = [
    ["Hacker Hugs a Tree"], ["Hacker's Bright Idea"], ["Hackerized!"],
    ["Inside Hacker"], ["Peace, Love, and Hackerness"], ["The Hacker's Challenge"],
]

SALARY_12_TO_15 = [
    [1998, 14936667], [2001, 13650000], [2001, 13571429], [2000, 13350000],
    [2001, 13350000], [2001, 13166667], [2001, 13050000], [2001, 13000000],
    [2000, 12868670], [2001, 12600000], [2001, 12500000], [2001, 12500000],
    [2001, 12500000], [2000, 12357143], [2001, 12357143], [2001, 12166667],
    [2001, 12166667], [2000, 12142857], [2000, 12071429], [2001, 12049040],
    [2000, 12000000],
]


def build_cases(challenge_db: str | None, mutation_db: str | None) -> list[Case]:
    cases: list[Case] = [
        Case("R01", "worded numeric LIMIT", "cyberchase.db", "episodes",
             "Give me a dozen episode titles in ID order.", expected_count=12,
             sql_contains=["LIMIT"]),
        Case("R02", "text contains", "cyberchase.db", "episodes",
             "List episode titles containing the word Hacker, alphabetically.",
             expected_rows=HACKER_TITLES, params_contains=["%Hacker%"]),
        Case("R03", "COUNT DISTINCT", "cyberchase.db", "episodes",
             "How many different topics are represented?", expected_scalar=113,
             sql_contains=["COUNT(DISTINCT"]),
        Case("R04", "GROUP BY + HAVING + calculated ordering + tie ordering", "cyberchase.db", "episodes",
             "Which seasons have at least 10 episodes? Show season and episode count, busiest first, and use the lower season number first when counts tie.",
             expected_rows=[[1,26],[2,14],[3,12],[12,12],[4,10],[5,10],[6,10],[11,10],[13,10]],
             sql_contains=["GROUP BY", "HAVING", "ORDER BY"]),
        Case("R05", "year-only condition on ISO date", "cyberchase.db", "episodes",
             "How many episodes aired in 2020?", expected_scalar=9,
             sql_not_contains=["LIKE"], params_contains=["2020-01-01", "2020-12-31"]),
        Case("R06", "digit + scale inclusive numeric range", "moneyball.db", "salaries",
             "Give me salaries no lower than 12 million and no higher than 15 million. Return year and salary, ordered from largest to smallest.",
             expected_rows=SALARY_12_TO_15, params_contains=[12000000, 15000000]),
        Case("R07", "inclusive year range", "moneyball.db", "salaries",
             "How many salary records are from 1998 through 2001 inclusive?",
             expected_scalar=3700, params_contains=[1998, 2001]),
        Case("R08", "grouped filter + COUNT + limit", "moneyball.db", "performances",
             "Considering only performances with at least 20 home runs, which five years had the most such performances? Show the year and how many there were.",
             expected_rows=[[1999,102],[2000,97],[2001,87],[1996,81],[1998,81]],
             sql_contains=["GROUP BY", "ORDER BY", "LIMIT"]),
        Case("R09", "direct one-hop FK join", "moneyball.db", "salaries",
             "Show the five highest salary records for players born in the USA. Return first name, last name, year, and salary, highest salary first.",
             expected_count=5, expected_joined_tables=["salaries", "players"],
             sql_contains=[" JOIN "], params_contains=["USA", 5]),
        Case("R10", "same-column alternatives + two grouping columns", "dese.db", "schools",
             "For Boston and Cambridge, break down the number of schools by city and school type.",
             expected_rows=[["Boston","Charter School",5],["Boston","Public School",10],
                            ["Cambridge","Charter School",3],["Cambridge","Public School",17]],
             sql_contains=["GROUP BY"]),
    ]

    if challenge_db:
        cases.extend([
            Case("C01", "all rows", challenge_db, "products",
                 "Show every product row, ordered alphabetically by name.", expected_count=12),
            Case("C02", "selected columns", challenge_db, "products",
                 "Show every product's name and price, alphabetically.", expected_count=12),
            Case("C03", "equality =", challenge_db, "orders",
                 "How many purchases were made by customers from Morocco?", expected_scalar=10),
            Case("C04", "not equal !=", challenge_db, "orders",
                 "How many purchases are not completed?", expected_scalar=7),
            Case("C05", "greater than >", challenge_db, "products",
                 "How many products cost more than 200?", expected_scalar=4),
            Case("C06", "greater/equal >=", challenge_db, "employees",
                 "How many employees have a performance score of at least 4.8?", expected_scalar=4),
            Case("C07", "less than <", challenge_db, "products",
                 "How many products have fewer than 10 units in stock?", expected_scalar=4),
            Case("C08", "less/equal <=", challenge_db, "tickets",
                 "How many tickets with a recorded satisfaction score have a score no higher than 3?",
                 expected_scalar=5),
            Case("C09", "inclusive numeric interval", challenge_db, "employees",
                 "How many employees earn between 70000 and 90000 inclusive?", expected_scalar=7),
            Case("C10", "inclusive date interval", challenge_db, "orders",
                 "How many purchases were placed from February 1, 2026 through February 28, 2026 inclusive?",
                 expected_scalar=8),
            Case("C11", "IS NULL", challenge_db, "tickets",
                 "How many tickets do not have a satisfaction score recorded?", expected_scalar=5),
            Case("C12", "IS NOT NULL", challenge_db, "orders",
                 "How many purchases have a shipping date recorded?", expected_scalar=26),
            Case("C13", "text prefix", challenge_db, "products",
                 "Show product names beginning with Beacon.", expected_rows=[["Beacon Laptop"]],
                 params_contains=["Beacon%"]),
            Case("C14", "text suffix", challenge_db, "products",
                 "Show product names ending in Mouse.", expected_rows=[["Cedar Mouse"]],
                 params_contains=["%Mouse"]),
            Case("C15", "cross-column AND", challenge_db, "employees",
                 "Show remote employees younger than 35, returning name, age, and performance, strongest performance first.",
                 expected_rows=[["Karim",31,4.9],["Hajar",28,4.9],["Sofia",34,4.8],["Salma",26,4.8],
                                ["Amal",29,4.7],["Imane",33,4.7],["Yassine",27,4.6],["Mehdi",30,4.0]],
                 params_contains=[35, 1]),
            Case("C16", "cross-column OR", challenge_db, "tickets",
                 "Show tickets whose status is open or whose priority is urgent, ordered by ID.",
                 expected_count=10, sql_contains=[" OR "], params_contains=["urgent", "open"]),
            Case("C17", "same-column alternatives", challenge_db, "employees",
                 "Show employees who live in Casablanca or Oujda, returning name and city, ordered by city and then name.",
                 expected_rows=[["Amal","Casablanca"],["Hassan","Casablanca"],["Leila","Casablanca"],
                                ["Othman","Casablanca"],["Salma","Casablanca"],["Sofia","Casablanca"],
                                ["Hajar","Oujda"],["Karim","Oujda"],["Mehdi","Oujda"],["Yassine","Oujda"]]),
            Case("C18", "ascending ORDER BY + LIMIT", challenge_db, "products",
                 "Show the five lowest product prices.",
                 expected_rows=[[36.0],[49.0],[79.0],[89.0],[119.0]]),
            Case("C19", "descending ORDER BY + LIMIT", challenge_db, "products",
                 "Show the five highest product prices.",
                 expected_rows=[[1299.0],[699.0],[329.0],[249.0],[189.0]]),
            Case("C19P", "entity + attribute projection under superlative wording", challenge_db, "products",
                 "Show the five cheapest products with their prices.",
                 expected_rows=[["Cedar Mouse",36.0],["Ion Charger",49.0],["Flux Webcam",79.0],
                                ["Atlas Keyboard",89.0],["Halo Headset",119.0]]),
            Case("C20", "DISTINCT projection", challenge_db, "products",
                 "List the different product categories alphabetically, showing each only once.",
                 expected_rows=[["Accessories"],["Computers"],["Displays"],["Networking"],["Office"],["Storage"]],
                 sql_contains=["DISTINCT"]),
            Case("C21", "COUNT", challenge_db, "tickets",
                 "How many tickets are resolved?", expected_scalar=15),
            Case("C22", "COUNT DISTINCT", challenge_db, "orders",
                 "How many different customer countries are represented?", expected_scalar=5,
                 sql_contains=["COUNT(DISTINCT"]),
            Case("C23", "AVG", challenge_db, "products",
                 "What is the average price of products that are not discontinued?", expected_scalar=309.7),
            Case("C24", "SUM", challenge_db, "orders",
                 "What is the total quantity across completed purchases?", expected_scalar=71),
            Case("C25", "MIN", challenge_db, "products",
                 "What is the lowest price among products that are not discontinued?", expected_scalar=36.0),
            Case("C26", "MAX", challenge_db, "employees",
                 "What is the highest employee salary?", expected_scalar=125000),
            Case("C27", "rounded aggregate", challenge_db, "employees",
                 "What is the average employee performance score rounded to three decimal places?",
                 expected_scalar=4.533, sql_contains=["ROUND"]),
            Case("C28", "GROUP BY one column / grouped COUNT", challenge_db, "employees",
                 "How many employees are in each department? Order departments alphabetically.",
                 expected_rows=[["Engineering",5],["Finance",3],["Sales",4],["Support",3]],
                 sql_contains=["GROUP BY"]),
            Case("C29", "GROUP BY two columns + HAVING", challenge_db, "employees",
                 "Show every city and department combination with at least two employees, together with the employee count. Order by city and department.",
                 expected_rows=[["Casablanca","Engineering",2],["Casablanca","Sales",2],["Rabat","Engineering",2]],
                 sql_contains=["GROUP BY", "HAVING"]),
            Case("C30", "grouped aggregate + rounding", challenge_db, "orders",
                 "For completed purchases, show each country and its average quantity per order rounded to two decimal places, alphabetically by country.",
                 expected_rows=[["Canada",2.0],["France",3.17],["Germany",3.17],["Morocco",2.75],["Spain",4.5]],
                 sql_contains=["AVG", "ROUND", "GROUP BY"]),
            Case("C31", "HAVING threshold", challenge_db, "orders",
                 "Which countries have at least four completed purchases? Show the country and count, highest count first.",
                 expected_rows=[["Morocco",8],["France",6],["Germany",6]], sql_contains=["HAVING"]),
            Case("C32", "group ordering by key", challenge_db, "orders",
                 "Count purchases by country and order the countries alphabetically.",
                 expected_rows=[["Canada",3],["France",7],["Germany",7],["Morocco",10],["Spain",3]]),
            Case("C33", "group ordering by calculated measure", challenge_db, "orders",
                 "Count purchases by country and order from the largest count to the smallest, alphabetically when counts tie.",
                 expected_rows=[["Morocco",10],["France",7],["Germany",7],["Canada",3],["Spain",3]]),
            Case("C34", "deterministic tie ordering", challenge_db, "employees",
                 "Count employees by department, largest department first, and when counts tie put the department name alphabetically first.",
                 expected_rows=[["Engineering",5],["Sales",4],["Finance",3],["Support",3]]),
            Case("C35", "year-only ISO date", challenge_db, "orders",
                 "How many purchases were placed in 2026?", expected_scalar=30,
                 sql_not_contains=["LIKE"], params_contains=["2026-01-01", "2026-12-31"]),
            # Boundary/unsupported claims should reject instead of executing a partial query.
            Case("U01", "unsupported nested Boolean expression", challenge_db, None,
                 "Show employees who are either remote engineers under 30 or non-remote sales employees over 35.",
                 expect_rejected=True),
            Case("U02", "unsupported arbitrary computed expression", challenge_db, None,
                 "For each completed purchase, calculate quantity multiplied by unit price and show the five largest calculated amounts.",
                 expect_rejected=True),
            Case("U03", "unsupported window/ranking", challenge_db, None,
                 "Rank every employee by salary within their department and show the top two ranks from each department.",
                 expect_rejected=True),
        ])

    if mutation_db:
        cases.extend([
            Case("M01", "UPDATE preview", mutation_db, "inventory",
                 "Set the price to 77.25 for the item whose SKU is UNIQUE.", mode="change",
                 expected_affected=1, expected_after_contains={"sku":"UNIQUE", "price":77.25}),
            Case("M02", "INSERT preview", mutation_db, "inventory",
                 "Add an inventory item with SKU AUTOTEST42, name Automated Test Item, status testing, stock 23, and price 81.75.",
                 mode="change", expected_affected=1,
                 expected_after_contains={"sku":"AUTOTEST42", "name":"Automated Test Item", "status":"testing", "stock":23, "price":81.75}),
            Case("M03", "DELETE preview", mutation_db, "inventory",
                 "Delete the inventory item whose SKU is UNIQUE.", mode="change", expected_affected=1),
            Case("M04", "mutation >100 row rejection", mutation_db, None,
                 "Set the stock to 0 for every item whose status is group101.", mode="change", expect_rejected=True),
            Case("M05", "explicit-values-only mutation rejection", mutation_db, None,
                 "Give the item whose SKU is UNIQUE a reasonable price.", mode="change", expect_rejected=True),
        ])

    return cases


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def close_enough(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)
    return a == b


def rows_equal(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, list) or not isinstance(expected, list) or len(actual) != len(expected):
        return False
    for ar, er in zip(actual, expected):
        if not isinstance(ar, (list, tuple)) or len(ar) != len(er):
            return False
        if not all(close_enough(av, ev) for av, ev in zip(ar, er)):
            return False
    return True


def contains_param(params: list[Any], expected: Any) -> bool:
    return any(close_enough(value, expected) for value in params)


def dict_contains(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(key in actual and close_enough(actual[key], value) for key, value in expected.items())


def validate_case(case: Case, run: dict[str, Any]) -> tuple[str, list[str]]:
    result = run.get("result")
    error = run.get("error")
    reasons: list[str] = []

    if case.expect_rejected:
        rejected = bool(error) or not isinstance(result, dict) or not result.get("supported", False)
        if rejected:
            return "PASS", ["rejected as expected"]
        return "FAIL", ["request was expected to be rejected but executed successfully"]

    if error:
        return "FAIL", [f"IntentSQL error: {error}"]
    if not isinstance(result, dict):
        return "FAIL", ["no result event returned"]
    if not result.get("supported", False):
        return "FAIL", ["IntentSQL marked this supported feature unsupported"]

    program = result.get("program") or {}
    base_table = program.get("base_table") or program.get("table")
    if case.expected_table and base_table != case.expected_table:
        reasons.append(f"wrong table: expected {case.expected_table!r}, got {base_table!r}")

    if case.expected_joined_tables is not None:
        actual_joined = program.get("joined_tables") or []
        if set(actual_joined) != set(case.expected_joined_tables):
            reasons.append(f"wrong joined_tables: expected {case.expected_joined_tables!r}, got {actual_joined!r}")

    rows = result.get("rows")
    if case.expected_scalar is not None:
        if not (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], (list, tuple)) and len(rows[0]) == 1):
            reasons.append(f"expected one scalar row, got {rows!r}")
        elif not close_enough(rows[0][0], case.expected_scalar):
            reasons.append(f"wrong scalar: expected {case.expected_scalar!r}, got {rows[0][0]!r}")

    if case.expected_rows is not None and not rows_equal(rows, case.expected_rows):
        reasons.append(f"rows differ from exact expected result ({len(case.expected_rows)} expected)")

    if case.expected_count is not None:
        count = result.get("total_rows", len(rows or []))
        if count != case.expected_count:
            reasons.append(f"wrong row count: expected {case.expected_count}, got {count}")

    if case.expected_affected is not None:
        if result.get("affected") != case.expected_affected:
            reasons.append(f"wrong affected count: expected {case.expected_affected}, got {result.get('affected')!r}")

    if case.expected_after_contains is not None:
        after = result.get("after") or []
        if not any(isinstance(row, dict) and dict_contains(row, case.expected_after_contains) for row in after):
            reasons.append(f"preview after rows do not contain {case.expected_after_contains!r}")

    sql = result.get("sql") or ""
    folded = sql.casefold()
    for fragment in case.sql_contains:
        if fragment.casefold() not in folded:
            reasons.append(f"SQL missing expected fragment {fragment!r}")
    for fragment in case.sql_not_contains:
        if fragment.casefold() in folded:
            reasons.append(f"SQL contains forbidden fragment {fragment!r}")

    params = list(result.get("params") or [])
    for expected in case.params_contains:
        if not contains_param(params, expected):
            reasons.append(f"bound parameters do not contain {expected!r}; got {params!r}")

    return ("PASS" if not reasons else "FAIL"), reasons


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def result_rows_text(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "<no result>"
    if result.get("rows") is not None:
        return safe_json(result.get("rows"))
    if result.get("before") is not None or result.get("after") is not None:
        return "BEFORE:\n" + safe_json(result.get("before")) + "\nAFTER:\n" + safe_json(result.get("after"))
    return "<no row payload>"


def case_report(case: Case, run: dict[str, Any], verdict: str, reasons: list[str]) -> str:
    result = run.get("result") if isinstance(run.get("result"), dict) else None
    stats = (result or {}).get("stats") or run.get("stats") or {}
    sql = (result or {}).get("sql") or ""
    params = (result or {}).get("params") or []
    lines = [
        "=" * 100,
        f"{case.id} [{verdict}] {case.feature}",
        "=" * 100,
        f"DATABASE       : {case.database}",
        f"EXPECTED TABLE : {case.expected_table or '(n/a)'}",
        f"MODE           : {case.mode}",
        f"PROMPT         : {case.prompt}",
        f"ELAPSED        : {run.get('elapsed_s', 0):.2f}s",
    ]
    if reasons:
        lines.append("CHECK          : " + " | ".join(reasons))
    if run.get("error"):
        lines.append(f"ERROR          : {run['error']}")
    lines += [
        "",
        "RAW SQL:",
        sql or "<none>",
        "",
        "BOUND PARAMETERS:",
        safe_json(params),
        "",
        "SQL WITH VALUES (DISPLAY ONLY):",
        bind_sql_for_display(sql, params) or "<none>",
        "",
        "PROGRAM:",
        safe_json((result or {}).get("program")),
        "",
        "RESULT / PREVIEW ROWS:",
        result_rows_text(result),
        "",
        "STATS:",
        safe_json(stats),
        "",
        "RETURNED COLUMNS:",
        safe_json((result or {}).get("columns")),
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Database discovery/import and non-NL API checks
# ---------------------------------------------------------------------------


def available_databases(base_url: str, timeout: float) -> set[str]:
    return {item["id"] for item in http_json(base_url, "/api/databases", timeout=timeout)}


def ensure_import(base_url: str, path: str | None, timeout: float) -> str | None:
    if not path:
        return None
    local = Path(path).expanduser().resolve()
    expected = local.stem.replace(" ", "_") + ".db"
    dbs = available_databases(base_url, timeout)
    # API sanitization only matters for unusual filenames. Prefer exact stem match.
    by_stem = {Path(name).stem: name for name in dbs}
    if local.stem in by_stem:
        return by_stem[local.stem]
    if not local.is_file():
        raise RuntimeError(f"Database file not found: {local}")
    try:
        result = http_json(base_url, "/api/databases", method="POST", body={"path": str(local)}, timeout=timeout)
        return result["id"]
    except RuntimeError as exc:
        # A previous import may exist under the sanitized name.
        dbs = available_databases(base_url, timeout)
        by_stem = {Path(name).stem: name for name in dbs}
        if local.stem in by_stem:
            return by_stem[local.stem]
        raise exc


def api_feature_checks(base_url: str, dbs: set[str], timeout: float) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for database in ("cyberchase.db", "dese.db", "moneyball.db"):
        if database not in dbs:
            checks.append({"feature": "bundled database present", "database": database, "pass": False,
                           "detail": "database missing from /api/databases"})
            continue
        schema = http_json(base_url, f"/api/schema/{database}", timeout=timeout)
        overview = http_json(base_url, f"/api/inspect/{database}/overview", timeout=timeout)
        checks.append({
            "feature": "schema + first-five-row inspector",
            "database": database,
            "pass": bool(schema.get("tables")) and bool(overview.get("tables")) and overview.get("preview_rows") == 5,
            "detail": {"tables": [t["name"] for t in schema.get("tables", [])],
                       "overview_preview_rows": overview.get("preview_rows")},
        })
    if "cyberchase.db" in dbs:
        q = http_json(base_url, "/api/inspect/cyberchase.db", method="POST",
                      body={"sql": 'SELECT title, season FROM episodes ORDER BY id LIMIT 3;'}, timeout=timeout)
        checks.append({"feature": "read-only inspector SQL console", "database": "cyberchase.db",
                       "pass": q.get("rows") == [["Lost My Marbles",1],["Castleblanca",1],["R-Fair City",1]],
                       "detail": q})
        try:
            http_json(base_url, "/api/inspect/cyberchase.db", method="POST",
                      body={"sql": 'DELETE FROM episodes;'}, timeout=timeout)
            denied = False
            detail = "write unexpectedly accepted"
        except Exception as exc:
            denied = True
            detail = str(exc)
        checks.append({"feature": "inspector rejects writes", "database": "cyberchase.db",
                       "pass": denied, "detail": detail})
    return checks


# ---------------------------------------------------------------------------
# Optional commit/undo smoke test
# ---------------------------------------------------------------------------


def commit_undo_check(base_url: str, mutation_db: str, timeout: float) -> dict[str, Any]:
    unique = f"UNDO{int(time.time())}"
    prompt = f"Add an inventory item with SKU {unique}, name Undo Test Item, status testing, stock 4, and price 12.50."
    run = run_sse(base_url, mutation_db, prompt, "change", timeout)
    result = run.get("result") if isinstance(run.get("result"), dict) else None
    check = {"feature": "commit + undo", "database": mutation_db, "prompt": prompt,
             "preview": result, "events": run.get("events"), "pass": False}
    if run.get("error") or not result or not result.get("supported") or result.get("affected") != 1:
        check["detail"] = "safe insert preview failed; nothing was committed"
        return check
    after = result.get("after") or []
    if not any(isinstance(row, dict) and row.get("sku") == unique for row in after):
        check["detail"] = "preview did not bind the unique SKU; refusing to commit"
        return check
    token = result.get("commit_token")
    if not token:
        check["detail"] = "no commit token returned"
        return check

    committed = http_json(base_url, "/api/commit", method="POST",
                          body={"token": token, "confirm": True}, timeout=timeout)
    present = http_json(base_url, f"/api/inspect/{mutation_db}", method="POST",
                        body={"sql": f"SELECT sku,name,status,stock,price FROM inventory WHERE sku='{unique}';"},
                        timeout=timeout)
    undo_token = committed.get("undo_token")
    undone = http_json(base_url, "/api/undo", method="POST", body={"token": undo_token}, timeout=timeout)
    absent = http_json(base_url, f"/api/inspect/{mutation_db}", method="POST",
                       body={"sql": f"SELECT sku FROM inventory WHERE sku='{unique}';"}, timeout=timeout)
    check.update({"commit": committed, "present_after_commit": present,
                  "undo": undone, "present_after_undo": absent})
    check["pass"] = bool(present.get("rows")) and not absent.get("rows") and undone.get("restored") is True
    check["detail"] = "committed validated preview, verified row, then undid it" if check["pass"] else "commit/undo verification failed"
    return check


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end IntentSQL feature test suite")
    parser.add_argument("--base-url", default="http://127.0.0.1:7862")
    parser.add_argument("--challenge-db", help="Path to intentsql_challenge.db; auto-imported if needed")
    parser.add_argument("--mutation-db", help="Path to intentsql_mutation_stress.db; auto-imported if needed")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per request timeout in seconds")
    parser.add_argument("--only", help="Comma-separated case IDs, e.g. R02,R09,C35")
    parser.add_argument("--commit-undo", action="store_true",
                        help="After validating a safe insert preview, commit it and immediately undo it")
    parser.add_argument("--output-prefix", default="intentsql_test_report")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    try:
        health = http_json(base, "/api/health", timeout=args.timeout)
        connection = http_json(base, "/api/connection", timeout=args.timeout)
        initial_dbs = available_databases(base, args.timeout)
    except Exception as exc:
        print(f"ERROR: Cannot reach IntentSQL at {base}: {exc}", file=sys.stderr)
        return 2

    challenge_name = ensure_import(base, args.challenge_db, args.timeout) if args.challenge_db else (
        "intentsql_challenge.db" if "intentsql_challenge.db" in initial_dbs else None)
    mutation_name = ensure_import(base, args.mutation_db, args.timeout) if args.mutation_db else (
        "intentsql_mutation_stress.db" if "intentsql_mutation_stress.db" in initial_dbs else None)
    dbs = available_databases(base, args.timeout)

    cases = build_cases(challenge_name, mutation_name)
    if args.only:
        wanted = {item.strip().upper() for item in args.only.split(",") if item.strip()}
        cases = [case for case in cases if case.id.upper() in wanted]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    txt_path = Path(f"{args.output_prefix}_{stamp}.txt")
    json_path = Path(f"{args.output_prefix}_{stamp}.json")

    raw_report: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "base_url": base,
        "health": health,
        "connection": connection,
        "databases": sorted(dbs),
        "challenge_database": challenge_name,
        "mutation_database": mutation_name,
        "api_feature_checks": [],
        "cases": [],
        "commit_undo": None,
    }

    text_parts = [
        "INTENTSQL END-TO-END FEATURE REPORT",
        "=" * 100,
        f"Started        : {raw_report['started_at']}",
        f"Server         : {base}",
        f"Version        : {health.get('version')}",
        f"Provider       : {connection.get('name')} ({connection.get('provider')})",
        f"Model          : {connection.get('model')}",
        f"Databases      : {', '.join(sorted(dbs))}",
        f"Challenge DB   : {challenge_name or 'NOT AVAILABLE — challenge cases skipped'}",
        f"Mutation DB    : {mutation_name or 'NOT AVAILABLE — mutation cases skipped'}",
        "",
    ]

    print(f"IntentSQL {health.get('version')} @ {base}")
    print(f"Provider: {connection.get('name')} / {connection.get('model')}")
    print(f"Running {len(cases)} NL cases...\n")

    # Non-natural-language API/UI-backend claims.
    try:
        api_checks = api_feature_checks(base, dbs, args.timeout)
    except Exception as exc:
        api_checks = [{"feature": "API feature checks", "database": None, "pass": False, "detail": str(exc)}]
    raw_report["api_feature_checks"] = api_checks
    text_parts += ["API / INSPECTOR CHECKS", "-" * 100]
    for check in api_checks:
        status = "PASS" if check["pass"] else "FAIL"
        text_parts.append(f"[{status}] {check['feature']} · {check.get('database') or ''}\n{safe_json(check.get('detail'))}\n")
        print(f"[{status}] API: {check['feature']}")

    totals = {"PASS": 0, "FAIL": 0}
    usage_total = {"jev_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    for index, case in enumerate(cases, 1):
        if case.database not in dbs:
            run = {"events": [], "elapsed_s": 0, "result": None,
                   "error": f"database {case.database!r} is not registered in IntentSQL", "stats": None}
        else:
            print(f"[{index:02d}/{len(cases):02d}] {case.id} {case.feature} ...", end=" ", flush=True)
            run = run_sse(base, case.database, case.prompt, case.mode, args.timeout)
        verdict, reasons = validate_case(case, run)
        totals[verdict] += 1
        if case.database in dbs:
            print(verdict)

        stats = ((run.get("result") or {}).get("stats") if isinstance(run.get("result"), dict) else None) or run.get("stats") or {}
        for key in ("jev_calls", "input_tokens", "output_tokens"):
            usage_total[key] += int(stats.get(key) or 0)
        if isinstance(stats.get("cost_usd"), (int, float)):
            usage_total["cost_usd"] += float(stats["cost_usd"])

        record = {
            "id": case.id,
            "feature": case.feature,
            "database": case.database,
            "expected_table": case.expected_table,
            "mode": case.mode,
            "prompt": case.prompt,
            "verdict": verdict,
            "reasons": reasons,
            "expected": {
                "rows": case.expected_rows,
                "scalar": case.expected_scalar,
                "count": case.expected_count,
                "joined_tables": case.expected_joined_tables,
                "affected": case.expected_affected,
                "after_contains": case.expected_after_contains,
                "expect_rejected": case.expect_rejected,
                "sql_contains": case.sql_contains,
                "sql_not_contains": case.sql_not_contains,
                "params_contains": case.params_contains,
            },
            **run,
        }
        raw_report["cases"].append(record)
        text_parts.append(case_report(case, run, verdict, reasons))

    if args.commit_undo:
        if not mutation_name:
            cu = {"feature": "commit + undo", "pass": False,
                  "detail": "mutation database unavailable"}
        else:
            print("\nTesting guarded commit + undo...", end=" ", flush=True)
            try:
                cu = commit_undo_check(base, mutation_name, args.timeout)
            except Exception as exc:
                cu = {"feature": "commit + undo", "pass": False, "detail": str(exc)}
            print("PASS" if cu.get("pass") else "FAIL")
        raw_report["commit_undo"] = cu
        text_parts += ["", "COMMIT / UNDO CHECK", "-" * 100, safe_json(cu)]

    raw_report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    raw_report["summary"] = {**totals, "usage": usage_total,
                             "api_checks_pass": sum(bool(c.get("pass")) for c in api_checks),
                             "api_checks_total": len(api_checks)}

    summary = [
        "", "=" * 100, "SUMMARY", "=" * 100,
        f"NL cases PASS : {totals['PASS']}",
        f"NL cases FAIL : {totals['FAIL']}",
        f"API checks    : {sum(bool(c.get('pass')) for c in api_checks)}/{len(api_checks)} passed",
        f"Jev calls     : {usage_total['jev_calls']}",
        f"Input tokens  : {usage_total['input_tokens']}",
        f"Output tokens : {usage_total['output_tokens']}",
        f"Jev cost USD  : {usage_total['cost_usd']:.8f}",
        f"Text report   : {txt_path.resolve()}",
        f"JSON report   : {json_path.resolve()}",
    ]
    text_parts.extend(summary)

    txt_path.write_text("\n".join(text_parts) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(raw_report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    print("\n" + "\n".join(summary[2:]))
    return 0 if totals["FAIL"] == 0 and all(c.get("pass") for c in api_checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
