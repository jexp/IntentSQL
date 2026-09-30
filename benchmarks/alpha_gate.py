#!/usr/bin/env python3
"""
IntentSQL fresh alpha validation suite.

Purpose
-------
Run a NEW set of supported/claimed IntentSQL cases against the three bundled
databases and save full SSE traces + deterministic reference results to JSON.

This suite deliberately avoids the earlier A01-A33 wording. It mixes simple and
composed reads, joins, grouping, date/text semantics, mutations, INSERT, and
safe rejection.

Provider-stress defaults:
- 1 second between successful cases
- retry transient 429/5xx/520/network failures after 2 seconds
- 3 attempts per case
- 300 second timeout

Mutations are PREVIEW ONLY. This script NEVER calls the commit endpoint.

Usage
-----
    python benchmarks/alpha_gate.py

Run selected cases:
    python benchmarks/alpha_gate.py --ids B01,B09,B21

Resume after provider trouble:
    python benchmarks/alpha_gate.py --resume PATH_TO_SAVED_RESULTS.json

Gentler provider settings:
    python benchmarks/alpha_gate.py --between 2 --retry-wait 3 --retries 4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:7862"

TRANSIENT_MARKERS = (
    "http 408", "http 409", "http 425", "http 429",
    "http 500", "http 502", "http 503", "http 504",
    "http 520", "http 521", "http 522", "http 523", "http 524",
    "timeout", "timed out", "temporarily unavailable", "overloaded",
    "service unavailable", "connection reset", "remote end closed",
    "connection aborted", "connection refused",
)

FAIL_CLOSED_MARKERS = (
    "ambiguous", "unclear", "unsupported", "not supported", "cannot",
    "no clear", "beyond the supported", "beyond the proven",
    "no partial query was executed", "requires",
)


@dataclass(frozen=True)
class Case:
    id: str
    database: str
    prompt: str
    feature: str
    mode: str = "auto"

    reference_sql: str | None = None
    reference_params: tuple[Any, ...] = ()
    compare_rows: bool = False
    compare_unordered: bool = False

    expected_count_sql: str | None = None
    expected_count_params: tuple[Any, ...] = ()

    expected_affected_sql: str | None = None
    expected_affected_params: tuple[Any, ...] = ()
    expected_affected: int | None = None

    expect_rejected: bool = False
    reject_markers: tuple[str, ...] = ()
    expect_constraint_block: bool = False

    sql_contains: tuple[str, ...] = ()
    sql_not_contains: tuple[str, ...] = ()

    max_calls_warn: int | None = None
    notes: str = ""


CASES: tuple[Case, ...] = (
    # ================================================================
    # MONEYBALL — fresh wording, simple -> composed
    # ================================================================
    Case(
        "B01", "moneyball.db",
        "Count all players who throw left-handed.",
        "simple categorical equality + COUNT",
        reference_sql='SELECT COUNT(*) FROM "players" WHERE "throws" = \'L\'',
        compare_rows=True,
        max_calls_warn=7,
    ),
    Case(
        "B02", "moneyball.db",
        "Show first name, last name and height for players taller than 78 inches, shortest first, then last name.",
        "numeric comparison + explicit projection + supported secondary ordering",
        reference_sql=(
            'SELECT "first_name", "last_name", "height" FROM "players" '
            'WHERE "height" > 78 '
            'ORDER BY "height" ASC, "last_name" ASC'
        ),
        compare_rows=True,
        sql_contains=(">", "ORDER BY"),
        max_calls_warn=13,
    ),
    Case(
        "B03", "moneyball.db",
        "List the first name, last name and birth year of the seven oldest players born after 1950, oldest first, then last name.",
        "filter + inverse age ranking + worded quantity + secondary ordering",
        reference_sql=(
            'SELECT "first_name", "last_name", "birth_year" FROM "players" '
            'WHERE "birth_year" > 1950 '
            'ORDER BY "birth_year" ASC, "last_name" ASC LIMIT 7'
        ),
        compare_rows=True,
        sql_contains=(">", "ORDER BY", "LIMIT"),
        max_calls_warn=14,
    ),
    Case(
        "B04", "moneyball.db",
        "How many players have no birth state recorded?",
        "IS NULL + COUNT",
        reference_sql='SELECT COUNT(*) FROM "players" WHERE "birth_state" IS NULL',
        compare_rows=True,
        sql_contains=("IS NULL",),
        max_calls_warn=8,
    ),
    Case(
        "B05", "moneyball.db",
        "Show players whose birth city starts with San. Return first name, last name and birth city, alphabetically by last name then first name.",
        "prefix text predicate + projection + secondary ordering",
        reference_sql=(
            'SELECT "first_name", "last_name", "birth_city" FROM "players" '
            'WHERE "birth_city" LIKE \'San%\' '
            'ORDER BY "last_name" ASC, "first_name" ASC'
        ),
        compare_rows=True,
        sql_contains=("LIKE", "ORDER BY"),
        max_calls_warn=14,
    ),
    Case(
        "B06", "moneyball.db",
        "Show distinct birth countries for players born after 1980, alphabetically.",
        "DISTINCT + comparison + ordering",
        reference_sql=(
            'SELECT DISTINCT "birth_country" FROM "players" '
            'WHERE "birth_year" > 1980 '
            'ORDER BY "birth_country" ASC'
        ),
        compare_rows=True,
        sql_contains=("DISTINCT", ">", "ORDER BY"),
        max_calls_warn=13,
    ),
    Case(
        "B07", "moneyball.db",
        "For every birth year from 1970 through 1975, show the average player height, highest average first, then birth year.",
        "range + GROUP BY + AVG + aggregate ordering",
        reference_sql=(
            'SELECT "birth_year", AVG("height") FROM "players" '
            'WHERE "birth_year" BETWEEN 1970 AND 1975 '
            'GROUP BY "birth_year" '
            'ORDER BY AVG("height") DESC, "birth_year" ASC'
        ),
        compare_rows=True,
        sql_contains=("BETWEEN", "AVG", "GROUP BY", "ORDER BY"),
        max_calls_warn=15,
    ),
    Case(
        "B08", "moneyball.db",
        "Show the five highest salary records from 1995 through 2000 for players who bat left-handed. Return first name, last name, year and salary, highest salary first.",
        "join + predicates on both tables + grounded batting category + ranking",
        reference_sql=(
            'SELECT p."first_name", p."last_name", s."year", s."salary" '
            'FROM "salaries" s JOIN "players" p ON s."player_id" = p."id" '
            "WHERE s.\"year\" BETWEEN 1995 AND 2000 AND p.\"bats\" = 'L' "
            'ORDER BY s."salary" DESC LIMIT 5'
        ),
        compare_rows=True,
        sql_contains=("JOIN", "BETWEEN", "ORDER BY", "LIMIT"),
        max_calls_warn=16,
    ),

    # ================================================================
    # CYBERCHASE — dates, text, grouped aggregates, row extrema
    # ================================================================
    Case(
        "B09", "cyberchase.db",
        "How many episodes aired in 2022?",
        "year -> ISO date range + COUNT",
        reference_sql=(
            'SELECT COUNT(*) FROM "episodes" '
            'WHERE "air_date" BETWEEN \'2022-01-01\' AND \'2022-12-31\''
        ),
        compare_rows=True,
        sql_contains=("BETWEEN",),
        max_calls_warn=7,
    ),
    Case(
        "B10", "cyberchase.db",
        "Show title and air date for episodes aired after 2021-06-01, oldest first, then title.",
        "strict date comparison + projection + ordering",
        reference_sql=(
            'SELECT "title", "air_date" FROM "episodes" '
            'WHERE "air_date" > \'2021-06-01\' '
            'ORDER BY "air_date" ASC, "title" ASC'
        ),
        compare_rows=True,
        sql_contains=(">", "ORDER BY"),
        sql_not_contains=("BETWEEN",),
        max_calls_warn=11,
    ),
    Case(
        "B11", "cyberchase.db",
        "Show episode titles beginning with The, alphabetically.",
        "prefix text matching",
        reference_sql=(
            'SELECT "title" FROM "episodes" '
            'WHERE "title" LIKE \'The%\' ORDER BY "title" ASC'
        ),
        compare_rows=True,
        sql_contains=("LIKE", "ORDER BY"),
        max_calls_warn=10,
    ),
    Case(
        "B12", "cyberchase.db",
        "Show episode titles ending with Trouble, alphabetically.",
        "suffix text matching",
        reference_sql=(
            'SELECT "title" FROM "episodes" '
            'WHERE "title" LIKE \'%Trouble\' ORDER BY "title" ASC'
        ),
        compare_rows=True,
        sql_contains=("LIKE", "ORDER BY"),
        max_calls_warn=10,
    ),
    Case(
        "B13", "cyberchase.db",
        "Count episodes by topic, excluding missing topics, largest count first, then topic alphabetically.",
        "GROUP BY + COUNT + NULL exclusion + ordering",
        reference_sql=(
            'SELECT "topic", COUNT(*) FROM "episodes" '
            'WHERE "topic" IS NOT NULL '
            'GROUP BY "topic" ORDER BY COUNT(*) DESC, "topic" ASC'
        ),
        compare_rows=True,
        sql_contains=("IS NOT NULL", "COUNT", "GROUP BY", "ORDER BY"),
        max_calls_warn=13,
    ),
    Case(
        "B14", "cyberchase.db",
        "For each season, show the earliest air date, ordered by season.",
        "grouped MIN aggregate",
        reference_sql=(
            'SELECT "season", MIN("air_date") FROM "episodes" '
            'GROUP BY "season" ORDER BY "season" ASC'
        ),
        compare_rows=True,
        sql_contains=("MIN", "GROUP BY", "ORDER BY"),
        max_calls_warn=12,
    ),
    Case(
        "B15", "cyberchase.db",
        "Show the first aired episode from every season. Return season, title and air date, ordered by season then title.",
        "per-group whole-row earliest by air date",
        reference_sql=(
            'SELECT e."season", e."title", e."air_date" FROM "episodes" e '
            'WHERE e."air_date" = ('
            'SELECT MIN(e2."air_date") FROM "episodes" e2 '
            'WHERE e2."season" = e."season") '
            'ORDER BY e."season" ASC, e."title" ASC'
        ),
        compare_rows=True,
        sql_contains=("MIN", "ORDER BY"),
        max_calls_warn=11,
        notes="Ties are preserved.",
    ),

    # ================================================================
    # DESE — categories, two-column grouping, related human labels
    # ================================================================
    Case(
        "B16", "dese.db",
        "How many schools are in Boston?",
        "simple categorical predicate + COUNT",
        reference_sql='SELECT COUNT(*) FROM "schools" WHERE "city" = \'Boston\'',
        compare_rows=True,
        max_calls_warn=7,
    ),
    Case(
        "B17", "dese.db",
        "Show the names of public schools in Cambridge alphabetically.",
        "two categorical predicates + projection + ordering",
        reference_sql=(
            'SELECT "name" FROM "schools" '
            'WHERE "city" = \'Cambridge\' AND "type" = \'Public School\' '
            'ORDER BY "name" ASC'
        ),
        compare_rows=True,
        sql_contains=("ORDER BY",),
        max_calls_warn=12,
    ),
    Case(
        "B18", "dese.db",
        "Show the distinct school types, alphabetically.",
        "DISTINCT + ordering",
        reference_sql='SELECT DISTINCT "type" FROM "schools" ORDER BY "type" ASC',
        compare_rows=True,
        sql_contains=("DISTINCT", "ORDER BY"),
        max_calls_warn=9,
    ),
    Case(
        "B19", "dese.db",
        "Count schools by type, largest count first.",
        "single-key GROUP BY + COUNT + ordering",
        reference_sql=(
            'SELECT "type", COUNT(*) FROM "schools" '
            'GROUP BY "type" ORDER BY COUNT(*) DESC, "type" ASC'
        ),
        compare_rows=True,
        sql_contains=("COUNT", "GROUP BY", "ORDER BY"),
        max_calls_warn=13,
    ),
    Case(
        "B20", "dese.db",
        "Show the ten districts with the highest per-pupil expenditure. Return district name and expenditure, highest first.",
        "direct join + human label + ranking + limit",
        reference_sql=(
            'SELECT d."name", e."per_pupil_expenditure" '
            'FROM "expenditures" e '
            'JOIN "districts" d ON e."district_id" = d."id" '
            'ORDER BY e."per_pupil_expenditure" DESC LIMIT 10'
        ),
        compare_rows=True,
        sql_contains=("JOIN", "ORDER BY", "LIMIT"),
        max_calls_warn=12,
    ),

    # ================================================================
    # MUTATIONS — preview only, never committed
    # ================================================================
    Case(
        "B21", "cyberchase.db",
        "Delete episodes aired before 2003-01-01.",
        "DELETE strict date predicate preview",
        mode="change",
        expected_affected_sql=(
            'SELECT COUNT(*) FROM "episodes" WHERE "air_date" < ?'
        ),
        expected_affected_params=("2003-01-01",),
        sql_contains=("DELETE", "WHERE", "<"),
        max_calls_warn=7,
    ),
    Case(
        "B22", "dese.db",
        "Delete schools in Boston that are charter schools.",
        "DELETE two categorical predicates preview",
        mode="change",
        expected_affected_sql=(
            'SELECT COUNT(*) FROM "schools" WHERE "city" = ? AND "type" = ?'
        ),
        expected_affected_params=("Boston", "Charter School"),
        expect_constraint_block=True,
        sql_contains=("DELETE", "WHERE"),
        max_calls_warn=8,
    ),
    Case(
        "B23", "cyberchase.db",
        "Update episodes from season 3 and set topic to TEST.",
        "UPDATE equality predicate + SET assignment preview",
        mode="change",
        expected_affected_sql='SELECT COUNT(*) FROM "episodes" WHERE "season" = ?',
        expected_affected_params=(3,),
        sql_contains=("UPDATE", "SET", "WHERE"),
        max_calls_warn=8,
    ),
    Case(
        "B24", "cyberchase.db",
        'Insert an episode with season 98, episode number 2, title "Release Check", topic "Testing", air date "2098-05-10", and production code "RC98".',
        "INSERT full schema-grounded assignment preview",
        mode="change",
        expected_affected=1,
        sql_contains=("INSERT", "air_date"),
        max_calls_warn=8,
        notes="Preview only. Explicit air_date must not be dropped.",
    ),

    Case(
        "B27", "cyberchase.db",
        "For every season, return the first numbered episode. Show season, title and episode number, ordered by season.",
        "per-group row extremum + paraphrased output-field order",
        reference_sql=(
            'SELECT e."season", e."title", e."episode_in_season" FROM "episodes" e '
            'WHERE e."episode_in_season" = ('
            'SELECT MIN(e2."episode_in_season") FROM "episodes" e2 '
            'WHERE e2."season" = e."season") '
            'ORDER BY e."season" ASC'
        ),
        compare_rows=True,
        sql_contains=("MIN", "ORDER BY"),
        max_calls_warn=11,
    ),

    # ================================================================
    # SAFE REJECTION — also part of the alpha contract
    # ================================================================
    Case(
        "B25", "dese.db",
        "Show the happiest schools.",
        "ungrounded subjective metric must fail closed",
        expect_rejected=True,
        reject_markers=FAIL_CLOSED_MARKERS,
        max_calls_warn=8,
    ),
    Case(
        "B26", "moneyball.db",
        "Show the average of each year's maximum home-run total.",
        "unsupported nested aggregation must fail closed",
        expect_rejected=True,
        reject_markers=("nested", "unsupported", "beyond the supported", "no partial"),
        max_calls_warn=8,
    ),
)


def find_repo_root() -> Path:
    candidates = [Path.cwd(), Path(__file__).resolve().parent]
    candidates += list(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "data" / "moneyball.db").exists() and (candidate / "intentsql").is_dir():
            return candidate
    raise SystemExit(
        "Could not find IntentSQL repo root. Run from the repository root "
        "or place this script inside it."
    )


def shell(cmd: list[str], cwd: Path, timeout: float = 300.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:
        return 999, f"{type(exc).__name__}: {exc}"


def git_state(repo: Path) -> dict[str, Any]:
    rc, head = shell(["git", "rev-parse", "--short", "HEAD"], repo)
    rc2, status = shell(["git", "status", "--short"], repo)
    return {
        "head": head if rc == 0 else None,
        "dirty": bool(status.strip()) if rc2 == 0 else None,
        "status": status if rc2 == 0 else None,
    }


def server_pid_and_cwd(port: int) -> tuple[int | None, str | None]:
    try:
        proc = subprocess.run(
            ["ss", "-ltnp"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5
        )
        for line in proc.stdout.splitlines():
            if f":{port} " not in line:
                continue
            m = re.search(r"pid=(\d+)", line)
            if m:
                pid = int(m.group(1))
                try:
                    cwd = os.path.realpath(f"/proc/{pid}/cwd")
                except OSError:
                    cwd = None
                return pid, cwd
    except Exception:
        pass
    return None, None


def http_json(base_url: str, path: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8")
        return json.loads(data) if data else None


def wait_for_server(base_url: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            health = http_json(base_url, "/api/health", timeout=5)
            if isinstance(health, dict) and health.get("ready"):
                return health
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    raise RuntimeError(f"Server not ready after {timeout:g}s. Last error: {last}")


def run_offline_suite(repo: Path) -> dict[str, Any]:
    started = time.perf_counter()
    rc, output = shell(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test*.py"],
        repo,
    )
    return {
        "passed": rc == 0,
        "returncode": rc,
        "elapsed_s": round(time.perf_counter() - started, 3),
        "tail": "\n".join(output.splitlines()[-20:]),
    }


def run_sse(base_url: str, case: Case, timeout: float) -> dict[str, Any]:
    payload = json.dumps({
        "database": case.database,
        "prompt": case.prompt,
        "mode": case.mode,
    }).encode("utf-8")

    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/run",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )

    events: list[dict[str, Any]] = []
    started = time.perf_counter()
    http_error = None
    transport_error = None

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    events.append({"kind": "malformed_event", "raw": line[6:]})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        http_error = f"HTTP {exc.code}: {body}"
    except Exception as exc:
        transport_error = f"{type(exc).__name__}: {exc}"

    result = None
    event_error = None
    stats = None
    for event in events:
        if event.get("kind") == "result":
            result = event.get("result")
            if isinstance(result, dict):
                stats = result.get("stats") or stats
        elif event.get("kind") == "error":
            event_error = event.get("message") or "Unknown IntentSQL error"
            stats = event.get("stats") or stats
        elif event.get("stats"):
            stats = event.get("stats")

    return {
        "events": events,
        "result": result,
        "error": event_error or http_error or transport_error,
        "http_error": http_error,
        "transport_error": transport_error,
        "stats": stats,
        "elapsed_s": round(time.perf_counter() - started, 3),
    }


def is_transient(run: dict[str, Any]) -> bool:
    text = " ".join(
        str(x or "") for x in (
            run.get("error"),
            run.get("http_error"),
            run.get("transport_error"),
        )
    ).casefold()
    return any(marker in text for marker in TRANSIENT_MARKERS)


def execute_reference(repo: Path, database: str, sql: str, params: tuple[Any, ...]) -> list[list[Any]]:
    db = repo / "data" / database
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [list(row) for row in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def close_enough(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)
    return a == b


def rows_equal(a: Any, b: Any) -> bool:
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    for ar, br in zip(a, b):
        if not isinstance(ar, (list, tuple)) or len(ar) != len(br):
            return False
        if not all(close_enough(x, y) for x, y in zip(ar, br)):
            return False
    return True


def canonical_row(row: list[Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


def rows_equal_unordered(a: Any, b: Any) -> bool:
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    return sorted(canonical_row(x) for x in a) == sorted(canonical_row(x) for x in b)


def returned_count(result: dict[str, Any]) -> int | None:
    if isinstance(result.get("total_rows"), int):
        return result["total_rows"]
    rows = result.get("rows")
    return len(rows) if isinstance(rows, list) else None


def rejection_text(run: dict[str, Any]) -> str:
    chunks = [str(run.get("error") or "")]
    result = run.get("result")
    if isinstance(result, dict):
        chunks += [
            str(result.get("reason") or ""),
            json.dumps(result.get("vet") or {}, ensure_ascii=False),
        ]
    for event in run.get("events") or []:
        if event.get("kind") in ("error", "preview_blocked"):
            chunks.append(str(event.get("message") or event.get("reason") or ""))
    return " ".join(chunks).casefold()


def validate(repo: Path, case: Case, run: dict[str, Any]) -> tuple[str, list[str], list[str], dict[str, Any]]:
    result = run.get("result")
    error = run.get("error")
    reasons: list[str] = []
    warnings: list[str] = []
    reference: dict[str, Any] = {}

    if case.expect_rejected:
        supported = isinstance(result, dict) and result.get("supported") is True and not error
        if supported:
            return "FAIL", ["request should have failed closed but was supported"], warnings, reference

        text = rejection_text(run)
        markers = tuple(m.casefold() for m in case.reject_markers)
        if markers and not any(m in text for m in markers):
            reasons.append("request was rejected, but not for an expected capability/safety reason")
            reference["rejection_text"] = text[:2000]
        return ("PASS" if not reasons else "FAIL"), (
            ["failed closed as expected"] if not reasons else reasons
        ), warnings, reference

    if error:
        return "FAIL", [f"IntentSQL error: {error}"], warnings, reference
    if not isinstance(result, dict):
        return "FAIL", ["no result event"], warnings, reference

    sql = str(result.get("sql") or "")
    if case.expect_constraint_block:
        vet = result.get("vet") or {}
        reason = str(result.get("reason") or "")
        if vet.get("verdict") != "DATABASE_CONSTRAINT" and "constraint" not in reason.casefold():
            return "FAIL", ["expected database-constraint preview block was not observed"], warnings, reference
        for fragment in case.sql_contains:
            if fragment.casefold() not in sql.casefold():
                reasons.append(f"generated SQL missing {fragment!r}")
        rows = execute_reference(
            repo, case.database, case.expected_affected_sql, case.expected_affected_params
        ) if case.expected_affected_sql else []
        expected = int(rows[0][0]) if rows else case.expected_affected
        reference["expected_affected"] = expected
        if expected is not None and result.get("affected") != expected:
            reasons.append(
                f"wrong mutation preview count: expected {expected}, got {result.get('affected')!r}")
        return ("PASS" if not reasons else "FAIL"), (
            ["correct target preview blocked by database constraint"] if not reasons else reasons
        ), warnings, reference

    if result.get("supported") is not True:
        return "FAIL", ["supported alpha case was marked unsupported"], warnings, reference

    folded = sql.casefold()
    for fragment in case.sql_contains:
        if fragment.casefold() not in folded:
            reasons.append(f"generated SQL missing {fragment!r}")
    for fragment in case.sql_not_contains:
        if fragment.casefold() in folded:
            reasons.append(f"generated SQL unexpectedly contains {fragment!r}")

    if case.reference_sql:
        expected = execute_reference(repo, case.database, case.reference_sql, case.reference_params)
        reference["rows"] = expected
        if case.compare_rows:
            actual = result.get("rows")
            okay = (
                rows_equal_unordered(actual, expected)
                if case.compare_unordered
                else rows_equal(actual, expected)
            )
            if not okay:
                reasons.append(
                    f"rows differ from deterministic reference SQL "
                    f"({len(expected)} expected, {returned_count(result)} returned)"
                )

    if case.expected_count_sql:
        rows = execute_reference(
            repo, case.database, case.expected_count_sql, case.expected_count_params
        )
        expected_count = int(rows[0][0]) if rows else 0
        reference["expected_count"] = expected_count
        actual_count = returned_count(result)
        if actual_count != expected_count:
            reasons.append(f"wrong row count: expected {expected_count}, got {actual_count}")

    expected_affected = case.expected_affected
    if case.expected_affected_sql:
        rows = execute_reference(
            repo, case.database,
            case.expected_affected_sql,
            case.expected_affected_params,
        )
        expected_affected = int(rows[0][0]) if rows else 0
        reference["expected_affected"] = expected_affected

    if expected_affected is not None:
        actual = result.get("affected")
        if actual != expected_affected:
            reasons.append(
                f"wrong mutation preview count: expected {expected_affected}, got {actual!r}"
            )

    stats = run.get("stats") or {}
    calls = int(stats.get("jev_calls", stats.get("calls", 0)) or 0)
    if case.max_calls_warn is not None and calls > case.max_calls_warn:
        warnings.append(f"{calls} Jev calls > efficiency target {case.max_calls_warn}")

    return ("PASS" if not reasons else "FAIL"), reasons, warnings, reference


def load_resume(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        rec["case"]["id"]: rec
        for rec in doc.get("records", [])
        if isinstance(rec, dict) and rec.get("case", {}).get("id")
    }


def usage(run: dict[str, Any]) -> str:
    stats = run.get("stats") or {}
    calls = stats.get("jev_calls", stats.get("calls", "?"))
    inp = stats.get("input_tokens", "?")
    out = stats.get("output_tokens", "?")
    return f"calls={calls} tokens={inp}+{out} elapsed={run.get('elapsed_s')}s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--between", type=float, default=1.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-wait", type=float, default=2.0)
    parser.add_argument("--server-wait", type=float, default=60.0)
    parser.add_argument("--ids", default="")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--skip-offline", action="store_true")
    parser.add_argument("--allow-other-server-cwd", action="store_true")
    args = parser.parse_args()

    repo = find_repo_root()
    git = git_state(repo)

    m = re.search(r":(\d+)$", args.base_url)
    port = int(m.group(1)) if m else 7862
    pid, cwd = server_pid_and_cwd(port)
    if cwd and Path(cwd).resolve() != repo.resolve() and not args.allow_other_server_cwd:
        raise SystemExit(
            "RUNNING SERVER MISMATCH\n"
            f"tester repo: {repo}\n"
            f"server cwd : {cwd}\n"
            f"server pid : {pid}\n"
            "Restart Uvicorn from this repository before testing."
        )

    health = wait_for_server(args.base_url, args.server_wait)

    dbs = http_json(args.base_url, "/api/databases")
    available = {item.get("id") for item in dbs if isinstance(item, dict)}

    selected = CASES
    if args.ids.strip():
        wanted = {x.strip().upper() for x in args.ids.split(",") if x.strip()}
        selected = tuple(c for c in CASES if c.id in wanted)
        missing = wanted - {c.id for c in selected}
        if missing:
            parser.error("unknown IDs: " + ", ".join(sorted(missing)))

    needed = {c.database for c in selected}
    missing_dbs = needed - available
    if missing_dbs:
        raise SystemExit(f"Missing app databases: {sorted(missing_dbs)}")

    offline: dict[str, Any] = {"passed": None, "skipped": True}
    if not args.skip_offline:
        print("Running offline tests before spending Jev calls...")
        offline = run_offline_suite(repo)
        offline["skipped"] = False
        print(f"Offline: {'PASS' if offline['passed'] else 'FAIL'}")
        if not offline["passed"]:
            print(offline["tail"])
            return 1

    resume = load_resume(args.resume)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sys.path.insert(0, str(repo))
    from benchmarks.artifacts import artifact_root
    out = args.out or (artifact_root() / "release" / f"alpha-gate-{stamp}.json")

    print()
    print(f"IntentSQL {health.get('version', '?')} at {args.base_url}")
    print(f"Repo: {repo}")
    print(f"Git: {git.get('head')} dirty={git.get('dirty')}")
    if cwd:
        print(f"Server PID/CWD: {pid} / {cwd}")
    print(
        f"Fresh cases: {len(selected)} | timeout={args.timeout:g}s | "
        f"between={args.between:g}s | retries={args.retries} | "
        f"retry-wait={args.retry_wait:g}s"
    )
    print("Mutations are preview-only; this script never commits.\n")

    records: list[dict[str, Any]] = []

    for i, case in enumerate(selected, 1):
        old = resume.get(case.id)
        if old and old.get("verdict") == "PASS":
            print("=" * 90)
            print(f"[{i}/{len(selected)}] {case.id} — REUSED PASS")
            records.append(old)
            continue

        print("=" * 90)
        print(f"[{i}/{len(selected)}] {case.id} — {case.feature}")
        print(case.prompt)

        attempts: list[dict[str, Any]] = []
        final: dict[str, Any] | None = None
        provider_exhausted = False

        for attempt in range(1, max(args.retries, 1) + 1):
            if attempt > 1:
                print(f"  provider retry in {args.retry_wait:g}s...")
                time.sleep(max(0.0, args.retry_wait))
                wait_for_server(args.base_url, min(args.server_wait, 20))

            print(f"  attempt {attempt}/{args.retries} ...", flush=True)
            run = run_sse(args.base_url, case, args.timeout)
            attempts.append(run)
            final = run
            print(f"  {usage(run)}")

            if is_transient(run):
                print(f"  transient provider/network failure: {run.get('error')}")
                if attempt < args.retries:
                    continue
                provider_exhausted = True
            break

        assert final is not None

        if provider_exhausted:
            verdict = "PROVIDER"
            reasons = ["transient provider failure exhausted retries"]
            warnings: list[str] = []
            reference: dict[str, Any] = {}
        else:
            verdict, reasons, warnings, reference = validate(repo, case, final)

        result = final.get("result") if isinstance(final.get("result"), dict) else {}

        print(f"  => {verdict}")
        for reason in reasons:
            print(f"     {reason}")
        for warning in warnings:
            print(f"     WARN: {warning}")
        if result.get("sql"):
            print(f"  SQL: {result.get('sql')}")
            print(f"  params: {result.get('params') or []}")
        if result.get("affected") is not None:
            print(f"  affected preview: {result.get('affected')}")

        records.append({
            "case": asdict(case),
            "verdict": verdict,
            "reasons": reasons,
            "warnings": warnings,
            "reference": reference,
            "attempt_count": len(attempts),
            "attempts": attempts,
            "final": final,
        })

        checkpoint = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "base_url": args.base_url,
            "repo": str(repo),
            "git": git,
            "server": {"pid": pid, "cwd": cwd},
            "health": health,
            "offline": offline,
            "settings": {
                "timeout": args.timeout,
                "between": args.between,
                "retries": args.retries,
                "retry_wait": args.retry_wait,
            },
            "records": records,
        }
        out.write_text(
            json.dumps(checkpoint, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        if i < len(selected) and args.between > 0:
            time.sleep(args.between)

    counts = {
        key: sum(r["verdict"] == key for r in records)
        for key in ("PASS", "FAIL", "PROVIDER")
    }
    warning_count = sum(len(r.get("warnings") or []) for r in records)

    total_calls = total_in = total_out = 0
    durations: list[float] = []
    for rec in records:
        final = rec.get("final") or {}
        stats = final.get("stats") or {}
        total_calls += int(stats.get("jev_calls", stats.get("calls", 0)) or 0)
        total_in += int(stats.get("input_tokens", 0) or 0)
        total_out += int(stats.get("output_tokens", 0) or 0)
        if isinstance(final.get("elapsed_s"), (int, float)):
            durations.append(float(final["elapsed_s"]))

    durations.sort()
    median = durations[len(durations) // 2] if durations else None
    p95 = durations[min(len(durations) - 1, math.ceil(len(durations) * 0.95) - 1)] if durations else None

    if counts["FAIL"]:
        gate = "NOT_READY"
    elif counts["PROVIDER"]:
        gate = "INCONCLUSIVE_PROVIDER"
    elif warning_count:
        gate = "PASS_WITH_EFFICIENCY_WARNINGS"
    else:
        gate = "FRESH_ALPHA_GATE_PASS"

    summary = {
        "release_gate": gate,
        "cases": len(records),
        **counts,
        "warnings": warning_count,
        "jev_calls": total_calls,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "median_case_seconds": median,
        "p95_case_seconds": p95,
    }

    doc = json.loads(out.read_text(encoding="utf-8"))
    doc["summary"] = summary
    out.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    print("\n" + "=" * 90)
    print("FINAL", json.dumps(summary, sort_keys=True))
    print(f"JSON: {out}")

    if counts["FAIL"]:
        print("\nSemantic failures:")
        for rec in records:
            if rec["verdict"] == "FAIL":
                print(f"  {rec['case']['id']}: {'; '.join(rec['reasons'])}")

    if counts["PROVIDER"]:
        print("\nProvider-inconclusive:")
        for rec in records:
            if rec["verdict"] == "PROVIDER":
                print(f"  {rec['case']['id']}")

    print("\nSend me the resulting JSON file.")
    return 1 if counts["FAIL"] else (2 if counts["PROVIDER"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
