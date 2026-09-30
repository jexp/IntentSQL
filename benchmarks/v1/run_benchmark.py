#!/usr/bin/env python3
"""IntentSQL benchmark v1 runner.

The corpus is intentionally frozen and capability-bounded:
  * 20 core supported cases
  * 20 paraphrase supported cases
  * 20 adversarial-but-supported cases
  * 20 well-defined requests that should be rejected safely

Supported cases are checked twice:
  1) semantic-plan checks against IntentSQL's typed program
  2) execution equivalence against independently executed reference SQL

The runner uses the real /api/run SSE endpoint, so provider calls, latency,
usage/cost accounting and UI-visible semantic trace data are exercised exactly
as they are in the application.

No external packages are required by this script itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from benchmarks.artifacts import artifact_root  # noqa: E402

DEFAULT_CORPUS = HERE / "cases.json"
RESULTS_DIR = artifact_root() / "v1/results"

SUITES = ("core", "paraphrase", "adversarial", "reject")
SUPPORTED_SUITES = frozenset(("core", "paraphrase", "adversarial"))
BENCH_DB_PREFIX = "intentbench_v1_"
MAX_REFERENCE_ROWS = 5000

# These patterns are forbidden only in the supported suites. Rejection cases
# deliberately contain them so we can measure safe failure.
FORBIDDEN_SUPPORTED_SQL = (
    " UNION ", " INTERSECT ", " EXCEPT ", " OVER(", " OVER (",
    " CASE ", " EXISTS ", " WITH RECURSIVE ",
)

INFRA_ERROR_MARKERS = (
    "api key", "connection settings", "http ", "timed out", "timeout",
    "connection refused", "could not resolve", "provider", "endpoint",
    "another run is in progress", "remote end closed", "ssl",
)


# ---------------------------------------------------------------------------
# HTTP / SSE
# ---------------------------------------------------------------------------


def http_json(base_url: str, path: str, *, method: str = "GET", body: Any = None,
              timeout: float = 30.0) -> Any:
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


def run_sse(base_url: str, database: str, prompt: str, timeout: float) -> dict[str, Any]:
    payload = json.dumps({"database": database, "prompt": prompt, "mode": "read"}).encode("utf-8")
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
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    events.append({"kind": "malformed_event", "raw": line[6:]})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {
            "events": events,
            "elapsed_s": time.perf_counter() - started,
            "result": None,
            "error": f"HTTP {exc.code}: {raw}",
            "stats": None,
        }
    except Exception as exc:
        return {
            "events": events,
            "elapsed_s": time.perf_counter() - started,
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
        "result": result,
        "error": error,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Frozen corpus / reference data validation
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def corpus_hash(corpus_path: Path) -> str:
    return sha256_file(corpus_path)


def read_reference(case: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    path = ROOT / "data" / case["database"]
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(case["reference_sql"])
        columns = [item[0] for item in cur.description or ()]
        rows = [list(row) for row in cur.fetchmany(MAX_REFERENCE_ROWS + 1)]
    if len(rows) > MAX_REFERENCE_ROWS:
        raise ValueError(f"{case['id']}: reference query exceeds {MAX_REFERENCE_ROWS} rows")
    return columns, rows


def supported_sql_lint(case: dict[str, Any]) -> list[str]:
    """Catch accidental benchmark cases outside the documented compiler envelope."""
    if case["expect"] != "execute":
        return []
    sql = " " + " ".join(case["reference_sql"].upper().split()) + " "
    errors: list[str] = []
    if not sql.lstrip().startswith("SELECT "):
        errors.append("supported reference SQL must be a SELECT")
    if ";" in case["reference_sql"].rstrip().rstrip(";"):
        errors.append("multiple SQL statements are not allowed")
    for marker in FORBIDDEN_SUPPORTED_SQL:
        if marker in sql:
            errors.append(f"supported case contains forbidden SQL construct: {marker.strip()}")
    if "(SELECT " in sql:
        errors.append("supported case contains a subquery")
    join_count = sql.count(" JOIN ")
    if join_count > 1:
        errors.append("supported case requires more than one join")
    caps = set(case.get("required_capabilities", ()))
    if join_count and ({"group_by", "having", "count", "count_distinct", "aggregate_avg",
                        "aggregate_max", "aggregate_min", "aggregate_sum"} & caps):
        errors.append("current direct-join path is field-returning only; aggregate/group join slipped into corpus")
    if join_count and "secondary_ordering" in caps:
        errors.append("current direct-join path does not support secondary ordering")
    if join_count and ({"and", "or"} & caps):
        errors.append("current direct-join path intentionally benchmarks at most one source predicate")
    return errors


def validate_corpus(corpus_path: Path, *, verbose: bool = True) -> dict[str, Any]:
    manifest = json.loads(corpus_path.read_text(encoding="utf-8"))
    cases = manifest.get("cases") or []
    errors: list[str] = []

    counts = Counter(case.get("suite") for case in cases)
    expected_counts = manifest.get("suite_counts") or {}
    if len(cases) != sum(expected_counts.values()):
        errors.append(f"case total {len(cases)} != declared {sum(expected_counts.values())}")
    for suite in SUITES:
        if counts[suite] != expected_counts.get(suite):
            errors.append(f"suite {suite}: {counts[suite]} cases != declared {expected_counts.get(suite)}")

    ids = [case.get("id") for case in cases]
    dup_ids = [item for item, n in Counter(ids).items() if n > 1]
    if dup_ids:
        errors.append(f"duplicate case IDs: {dup_ids}")
    prompts = [case.get("prompt") for case in cases]
    dup_prompts = [item for item, n in Counter(prompts).items() if n > 1]
    if dup_prompts:
        errors.append(f"duplicate prompts: {dup_prompts[:3]}")

    declared_caps = set(manifest.get("supported_capability_envelope") or ())
    for db, expected_hash in (manifest.get("database_sha256") or {}).items():
        path = ROOT / "data" / db
        if not path.is_file():
            errors.append(f"missing benchmark database: {db}")
            continue
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            errors.append(f"database hash mismatch for {db}: {actual_hash} != {expected_hash}")

    family_signature: dict[str, tuple[Any, ...]] = {}
    for case in cases:
        prefix = case.get("id", "<unknown>")
        if case.get("suite") not in SUITES:
            errors.append(f"{prefix}: unknown suite {case.get('suite')}")
            continue
        if case.get("expect") not in ("execute", "reject"):
            errors.append(f"{prefix}: expect must be execute or reject")
        if case.get("suite") == "reject" and case.get("expect") != "reject":
            errors.append(f"{prefix}: reject suite must expect rejection")
        if case.get("suite") in SUPPORTED_SUITES and case.get("expect") != "execute":
            errors.append(f"{prefix}: supported suite must expect execution")
        if not case.get("prompt") or not case.get("reference_sql"):
            errors.append(f"{prefix}: prompt/reference_sql missing")
            continue
        caps = set(case.get("required_capabilities") or ())
        unknown = caps - declared_caps
        if case.get("expect") == "execute" and unknown:
            errors.append(f"{prefix}: undeclared capabilities {sorted(unknown)}")
        errors.extend(f"{prefix}: {item}" for item in supported_sql_lint(case))
        if case.get("expect") == "reject" and not case.get("unsupported_capability"):
            errors.append(f"{prefix}: rejection case lacks unsupported_capability")

        try:
            _, rows = read_reference(case)
        except Exception as exc:
            errors.append(f"{prefix}: reference SQL failed: {exc}")
            continue
        if len(rows) != case.get("reference_row_count"):
            errors.append(f"{prefix}: frozen row count {case.get('reference_row_count')} != {len(rows)}")
        frozen_sample = case.get("reference_sample") or []
        if rows[: len(frozen_sample)] != frozen_sample:
            errors.append(f"{prefix}: frozen reference sample no longer matches database")

        family = case.get("family")
        if family:
            signature = (case["database"], case["reference_sql"], canonical_json(case.get("semantic", {})))
            if family in family_signature and family_signature[family] != signature:
                errors.append(f"{prefix}: paraphrase family {family} does not share one frozen meaning")
            family_signature[family] = signature

    if verbose:
        print(f"Corpus: {len(cases)} cases · " + " · ".join(f"{suite}={counts[suite]}" for suite in SUITES))
        print("Databases:")
        for db, digest in (manifest.get("database_sha256") or {}).items():
            print(f"  {db:18} {digest[:16]}…")
        if errors:
            print(f"PRE-FLIGHT FAILED: {len(errors)} issue(s)")
            for item in errors:
                print("  -", item)
        else:
            print("PRE-FLIGHT PASS: corpus, database hashes, reference SQL and capability envelope are consistent.")
    if errors:
        raise SystemExit(2)
    return manifest


# ---------------------------------------------------------------------------
# Isolated benchmark databases
# ---------------------------------------------------------------------------


def workspace_root() -> Path:
    if os.name == "nt":
        default = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "IntentSQL"
    else:
        default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "intentsql"
    legacy = Path.home() / ".local/share/intentsql"
    if not legacy.exists():
        legacy = Path.home() / ".local/share/jevql"
    chosen = os.environ.get("INTENTSQL_WORKSPACE") or os.environ.get("JEVQL_WORKSPACE")
    if chosen:
        return Path(chosen).expanduser()
    if legacy.exists() and not default.exists():
        return legacy
    return default


def prepare_isolated_databases(manifest: dict[str, Any]) -> tuple[dict[str, str], list[Path]]:
    target_dir = workspace_root() / "databases"
    target_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    created: list[Path] = []
    stamp = f"{os.getpid()}_{int(time.time())}"
    for source_name in manifest["database_sha256"]:
        source = ROOT / "data" / source_name
        target_name = f"{BENCH_DB_PREFIX}{stamp}_{source_name}"
        target = target_dir / target_name
        shutil.copy2(source, target)
        if sha256_file(target) != manifest["database_sha256"][source_name]:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to create exact benchmark copy of {source_name}")
        mapping[source_name] = target_name
        created.append(target)
    return mapping, created


# ---------------------------------------------------------------------------
# Result / semantic equivalence
# ---------------------------------------------------------------------------


def normalize_scalar(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        return round(value, 8)
    return value


def normalize_rows(rows: Iterable[Iterable[Any]]) -> list[tuple[Any, ...]]:
    return [tuple(normalize_scalar(value) for value in row) for row in rows]


def rows_equivalent(actual: list[list[Any]], gold: list[list[Any]], ordered: bool) -> bool:
    """Require same width/rows while allowing harmless SELECT-column reordering."""
    if len(actual) != len(gold):
        return False
    if not actual and not gold:
        return True
    if not actual or not gold:
        return False
    if len(actual[0]) != len(gold[0]):
        return False
    width = len(gold[0])
    a = normalize_rows(actual)
    g = normalize_rows(gold)
    if width > 6:
        # Benchmark cases intentionally avoid wide projections. Refuse to hide a
        # mismatch behind factorial permutation search.
        return a == g if ordered else Counter(a) == Counter(g)
    for positions in permutations(range(width)):
        mapped = [tuple(row[pos] for pos in positions) for row in a]
        if (mapped == g) if ordered else (Counter(mapped) == Counter(g)):
            return True
    return False


def listify(value: Any) -> Any:
    if isinstance(value, tuple):
        return [listify(x) for x in value]
    if isinstance(value, list):
        return [listify(x) for x in value]
    if isinstance(value, dict):
        return {k: listify(v) for k, v in value.items()}
    return value


def subset_dict(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, value in expected.items():
        if key not in actual:
            return False
        if key == "value" and expected.get("operator") == actual.get("operator") == "IN":
            # SQL IN alternatives are a set; their presentation order has no
            # effect on the typed predicate or the returned rows.
            if {canonical_json(item) for item in listify(actual[key])} != {
                    canonical_json(item) for item in listify(value)}:
                return False
        elif listify(actual[key]) != listify(value):
            return False
    return True


def normalize_program(program: dict[str, Any] | None) -> dict[str, Any]:
    program = program or {}
    groups = []
    for item in program.get("groups") or []:
        if isinstance(item, dict):
            groups.append(item.get("group by") or item.get("column") or item)
        else:
            groups.append(item)
    return {
        "base_table": program.get("base_table"),
        "joined_tables": sorted(program.get("joined_tables") or []),
        "outputs": sorted(program.get("outputs") or []),
        "filters": sorted((listify(item) for item in (program.get("filters") or [])), key=canonical_json),
        "groups": groups,
        "having": sorted((listify(item) for item in (program.get("having") or [])), key=canonical_json),
        "order_by": listify(program.get("order_by") or []),
        "limit": program.get("limit"),
        "distinct": program.get("distinct"),
    }


def semantic_check(expected: dict[str, Any], program: dict[str, Any] | None) -> tuple[bool, list[str]]:
    actual = normalize_program(program)
    reasons: list[str] = []
    if "base_table" in expected and actual["base_table"] != expected["base_table"]:
        reasons.append(f"base_table {actual['base_table']!r} != {expected['base_table']!r}")
    if "joined_tables" in expected and set(actual["joined_tables"]) != set(expected["joined_tables"]):
        reasons.append(f"joined_tables {actual['joined_tables']!r} != {expected['joined_tables']!r}")
    if "outputs_contains" in expected:
        missing = [item for item in expected["outputs_contains"] if item not in (program or {}).get("outputs", [])]
        if missing:
            reasons.append(f"missing output(s): {missing}")
    if "filters_contains" in expected:
        actual_filters = program.get("filters", []) if program else []
        for wanted in expected["filters_contains"]:
            if not any(subset_dict(wanted, got) for got in actual_filters if isinstance(got, dict)):
                reasons.append(f"missing filter: {wanted}")
    if "groups" in expected and actual["groups"] != expected["groups"]:
        reasons.append(f"groups {actual['groups']!r} != {expected['groups']!r}")
    if "having_contains" in expected:
        actual_having = program.get("having", []) if program else []
        for wanted in expected["having_contains"]:
            if not any(subset_dict(wanted, got) for got in actual_having if isinstance(got, dict)):
                reasons.append(f"missing HAVING: {wanted}")
    if "order_by" in expected:
        wanted = expected["order_by"]
        got = actual["order_by"]
        if got[: len(wanted)] != wanted:
            reasons.append(f"order_by {got!r} does not start with {wanted!r}")
    if "limit" in expected and actual["limit"] != expected["limit"]:
        reasons.append(f"limit {actual['limit']!r} != {expected['limit']!r}")
    if "distinct" in expected and actual["distinct"] != expected["distinct"]:
        reasons.append(f"distinct {actual['distinct']!r} != {expected['distinct']!r}")
    return not reasons, reasons


def is_infrastructure_error(error: str | None) -> bool:
    text = (error or "").casefold()
    return any(marker in text for marker in INFRA_ERROR_MARKERS)


def rejection_pass(run: dict[str, Any]) -> tuple[bool, str]:
    result = run.get("result")
    error = run.get("error")
    if isinstance(result, dict):
        if result.get("supported") is False:
            return True, "explicit unsupported result"
        if result.get("supported") is True:
            return False, "UNSAFE: IntentSQL executed a case outside the frozen capability envelope"
    if error:
        if is_infrastructure_error(error):
            return False, "infrastructure/provider error is not a valid semantic rejection"
        return True, "rejected before SQL execution"
    return False, "no result and no semantic rejection error"


def reference_digest(rows: list[list[Any]]) -> str:
    return hashlib.sha256(canonical_json(listify(rows)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def summarize(records: list[dict[str, Any]], runs_per_case: int,
              wall_clock_s: float | None = None) -> dict[str, Any]:
    supported = [r for r in records if r["expect"] == "execute"]
    rejected = [r for r in records if r["expect"] == "reject"]
    by_suite: dict[str, Any] = {}
    for suite in SUITES:
        items = [r for r in records if r["suite"] == suite]
        by_suite[suite] = {
            "runs": len(items),
            "full_pass_runs": sum(bool(r["full_pass"]) for r in items),
            "accuracy": (sum(bool(r["full_pass"]) for r in items) / len(items)) if items else None,
        }

    case_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        case_groups[record["id"]].append(record)
    stable_cases = (sum(all(item["full_pass"] for item in group) and len(group) == runs_per_case
                        for group in case_groups.values()) if runs_per_case > 1 else None)

    family_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("family") and record["run_index"] == 1:
            family_groups[record["family"]].append(record)
    family_consistent = 0
    for group in family_groups.values():
        if group and all(item["full_pass"] for item in group):
            signatures = {canonical_json(normalize_program((item.get("result") or {}).get("program"))) for item in group}
            if len(signatures) == 1:
                family_consistent += 1

    latencies = [float(r["elapsed_s"]) for r in records if r.get("elapsed_s") is not None]
    stats = [r.get("stats") or {} for r in records]
    calls = [float(s.get("jev_calls", 0) or 0) for s in stats]
    input_tokens = [float(s.get("input_tokens", 0) or 0) for s in stats]
    output_tokens = [float(s.get("output_tokens", 0) or 0) for s in stats]
    total_tokens = [incoming + outgoing for incoming, outgoing in zip(input_tokens, output_tokens)]
    costs = [float(s.get("cost_usd", 0) or 0) for s in stats]

    return {
        "cases": len(case_groups),
        "runs": len(records),
        "runs_per_case": runs_per_case,
        "supported_run_execution_accuracy": (sum(bool(r.get("execution_pass")) for r in supported) / len(supported)) if supported else None,
        "supported_run_semantic_accuracy": (sum(bool(r.get("semantic_pass")) for r in supported) / len(supported)) if supported else None,
        "supported_run_full_accuracy": (sum(bool(r.get("full_pass")) for r in supported) / len(supported)) if supported else None,
        "safe_rejection_accuracy": (sum(bool(r.get("full_pass")) for r in rejected) / len(rejected)) if rejected else None,
        "unsafe_execution_rate": (sum(bool(r.get("unsafe_execution")) for r in rejected) / len(rejected)) if rejected else None,
        "stable_cases": stable_cases,
        "stable_case_rate": (stable_cases / len(case_groups)
                             if stable_cases is not None and case_groups else None),
        "paraphrase_families": len(family_groups),
        "paraphrase_families_consistent": family_consistent,
        "by_suite": by_suite,
        "latency_s": {
            "median": statistics.median(latencies) if latencies else None,
            "p95": percentile(latencies, .95),
        },
        "wall_clock_s": wall_clock_s,
        "jev": {
            "total_calls": int(sum(calls)),
            "total_input_tokens": int(sum(input_tokens)),
            "total_output_tokens": int(sum(output_tokens)),
            "total_tokens": int(sum(total_tokens)),
            "mean_calls": statistics.fmean(calls) if calls else None,
            "median_calls": statistics.median(calls) if calls else None,
            "mean_tokens": statistics.fmean(total_tokens) if total_tokens else None,
            "median_tokens": statistics.median(total_tokens) if total_tokens else None,
            "mean_input_tokens": statistics.fmean(input_tokens) if input_tokens else None,
            "mean_output_tokens": statistics.fmean(output_tokens) if output_tokens else None,
            "total_cost_usd": sum(costs),
            "mean_cost_usd": statistics.fmean(costs) if costs else None,
            "median_cost_usd": statistics.median(costs) if costs else None,
        },
    }


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def write_markdown(path: Path, metadata: dict[str, Any], summary: dict[str, Any],
                   records: list[dict[str, Any]]) -> None:
    lines = [
        "# IntentSQL Benchmark v1 Report",
        "",
        f"- Started: `{metadata['started_at']}`",
        f"- Provider: `{metadata.get('provider')}`",
        f"- Model: `{metadata.get('model')}`",
        f"- Git commit: `{metadata.get('git_commit') or 'unavailable'}`",
        f"- Corpus SHA256: `{metadata['corpus_sha256']}`",
        f"- Runs per case: **{summary['runs_per_case']}**",
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Supported execution accuracy | {pct(summary['supported_run_execution_accuracy'])} |",
        f"| Supported semantic-plan accuracy | {pct(summary['supported_run_semantic_accuracy'])} |",
        f"| Supported full accuracy | {pct(summary['supported_run_full_accuracy'])} |",
        f"| Safe rejection accuracy | {pct(summary['safe_rejection_accuracy'])} |",
        f"| Unsafe execution rate | {pct(summary['unsafe_execution_rate'])} |",
        f"| Stable case rate | {pct(summary['stable_case_rate'])} |",
        f"| Paraphrase families consistent | {summary['paraphrase_families_consistent']}/{summary['paraphrase_families']} |",
        f"| Benchmark wall-clock time | {summary['wall_clock_s']:.2f}s |" if summary['wall_clock_s'] is not None else "| Benchmark wall-clock time | n/a |",
        f"| Median latency | {summary['latency_s']['median']:.3f}s |" if summary['latency_s']['median'] is not None else "| Median latency | n/a |",
        f"| p95 latency | {summary['latency_s']['p95']:.3f}s |" if summary['latency_s']['p95'] is not None else "| p95 latency | n/a |",
        f"| Total Jev calls | {summary['jev']['total_calls']} |",
        f"| Total input tokens | {summary['jev']['total_input_tokens']} |",
        f"| Total output tokens | {summary['jev']['total_output_tokens']} |",
        f"| Total tokens | {summary['jev']['total_tokens']} |",
        f"| Mean / median calls per query | {summary['jev']['mean_calls']:.2f} / {summary['jev']['median_calls']:.2f} |" if summary['jev']['mean_calls'] is not None else "| Mean / median calls per query | n/a |",
        f"| Mean / median tokens per query | {summary['jev']['mean_tokens']:.1f} / {summary['jev']['median_tokens']:.1f} |" if summary['jev']['mean_tokens'] is not None else "| Mean / median tokens per query | n/a |",
        f"| Total estimated semantic cost | ${summary['jev']['total_cost_usd']:.6f} |",
        f"| Mean / median cost per query | ${summary['jev']['mean_cost_usd']:.6f} / ${summary['jev']['median_cost_usd']:.6f} |" if summary['jev']['mean_cost_usd'] is not None else "| Mean / median cost per query | n/a |",
        "",
        "## By suite",
        "",
        "| Suite | Passed runs | Total runs | Accuracy |",
        "|---|---:|---:|---:|",
    ]
    for suite in SUITES:
        item = summary["by_suite"][suite]
        lines.append(f"| {suite} | {item['full_pass_runs']} | {item['runs']} | {pct(item['accuracy'])} |")
    lines += ["", "## Failures", ""]
    failures = [r for r in records if not r["full_pass"]]
    if not failures:
        lines.append("No failures in this run.")
    else:
        for r in failures:
            reason = "; ".join(r.get("failure_reasons") or []) or r.get("error") or "unknown"
            lines.append(f"- **{r['id']}** run {r['run_index']} — {reason}")
    lines += [
        "",
        "## Method",
        "",
        "Supported cases must pass both the typed semantic-plan assertions and execution equivalence against frozen reference SQL. "
        "Expected-rejection cases pass only when IntentSQL refuses them before SQL execution; provider/network failures never count as valid rejections. "
        "Reference SQL is never supplied to IntentSQL.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def select_cases(manifest: dict[str, Any], suite: str | None, case_id: str | None) -> list[dict[str, Any]]:
    cases = manifest["cases"]
    if suite and suite != "all":
        cases = [case for case in cases if case["suite"] == suite]
    if case_id:
        cases = [case for case in cases if case["id"] == case_id]
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7862", help="running IntentSQL server")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--suite", choices=("all",) + SUITES, default="all")
    parser.add_argument("--case", help="run one frozen case ID")
    parser.add_argument("--runs", type=int, default=1, help="independent runs per case (release: 3)")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--keep-databases", action="store_true", help="keep isolated workspace copies for debugging")
    parser.add_argument("--no-events", action="store_true", help="omit full SSE/Jev trace from JSON report")
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")

    manifest = validate_corpus(args.corpus)
    selected = select_cases(manifest, args.suite, args.case)
    if not selected:
        parser.error("No benchmark cases selected")
    if args.preflight_only:
        print(f"Selected {len(selected)} case(s); no provider calls made.")
        return 0

    wall_started = time.perf_counter()
    health = http_json(args.url, "/api/health")
    connection = http_json(args.url, "/api/connection")
    if not health or not health.get("ready"):
        raise SystemExit("IntentSQL server is not ready")
    if connection.get("requires_key") and not connection.get("has_key"):
        raise SystemExit(f"{connection.get('name', 'Selected provider')} has no configured API key")

    db_map, created = prepare_isolated_databases(manifest)
    started_at = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    metadata = {
        "benchmark": manifest["benchmark"],
        "benchmark_version": manifest["version"],
        "started_at": started_at,
        "url": args.url,
        "server_version": health.get("version"),
        "provider": connection.get("name") or connection.get("provider"),
        "provider_id": connection.get("provider"),
        "model": connection.get("model"),
        "git_commit": git_commit(),
        "corpus_sha256": corpus_hash(args.corpus),
        "database_sha256": manifest["database_sha256"],
        "suite": args.suite,
        "case": args.case,
        "runs_per_case": args.runs,
    }

    try:
        for case in selected:
            _, gold_rows = read_reference(case)
            for run_index in range(1, args.runs + 1):
                run = run_sse(args.url, db_map[case["database"]], case["prompt"], args.timeout)
                result = run.get("result") if isinstance(run.get("result"), dict) else None
                failure_reasons: list[str] = []
                execution_pass = semantic_pass = False
                unsafe_execution = False
                semantic_reasons: list[str] = []
                rejection_basis = None

                if case["expect"] == "execute":
                    if result and result.get("supported") is True:
                        semantic_pass, semantic_reasons = semantic_check(case.get("semantic") or {}, result.get("program"))
                        execution_pass = rows_equivalent(result.get("rows") or [], gold_rows,
                                                         bool(case.get("result_ordered")))
                        if not semantic_pass:
                            failure_reasons.append("semantic plan: " + "; ".join(semantic_reasons))
                        if not execution_pass:
                            failure_reasons.append("execution result differs from frozen reference SQL")
                    else:
                        failure_reasons.append(run.get("error") or "supported case was not executed")
                    full_pass = bool(result and result.get("supported") is True and semantic_pass and execution_pass)
                else:
                    full_pass, rejection_basis = rejection_pass(run)
                    semantic_pass = full_pass
                    execution_pass = full_pass
                    unsafe_execution = bool(result and result.get("supported") is True)
                    if not full_pass:
                        failure_reasons.append(rejection_basis)

                stats = run.get("stats") or ((result or {}).get("stats") if result else None) or {}
                record = {
                    "id": case["id"],
                    "suite": case["suite"],
                    "family": case.get("family"),
                    "run_index": run_index,
                    "expect": case["expect"],
                    "database": case["database"],
                    "runtime_database": db_map[case["database"]],
                    "prompt": case["prompt"],
                    "required_capabilities": case.get("required_capabilities") or [],
                    "unsupported_capability": case.get("unsupported_capability"),
                    "full_pass": full_pass,
                    "semantic_pass": semantic_pass,
                    "execution_pass": execution_pass,
                    "unsafe_execution": unsafe_execution,
                    "semantic_reasons": semantic_reasons,
                    "failure_reasons": failure_reasons,
                    "rejection_basis": rejection_basis,
                    "elapsed_s": run.get("elapsed_s"),
                    "stats": stats,
                    "error": run.get("error"),
                    "result": result,
                    "reference": {
                        "row_count": len(gold_rows),
                        "sample": gold_rows[:5],
                        "rows_sha256": reference_digest(gold_rows),
                        "semantic": case.get("semantic") or {},
                    },
                }
                if not args.no_events:
                    record["events"] = run.get("events") or []
                records.append(record)
                label = "PASS" if full_pass else "FAIL"
                calls = stats.get("jev_calls", "-")
                cost = stats.get("cost_usd")
                cost_text = f"${cost:.6f}" if isinstance(cost, (int, float)) else "-"
                print(f"{label:4} {case['id']:18} run={run_index}/{args.runs} "
                      f"{run.get('elapsed_s', 0):6.2f}s calls={calls} cost={cost_text}", flush=True)
                if not full_pass:
                    print("     " + " | ".join(failure_reasons), flush=True)
    finally:
        if not args.keep_databases:
            for path in created:
                path.unlink(missing_ok=True)

    summary = summarize(records, args.runs, time.perf_counter() - wall_started)
    finished = datetime.now(timezone.utc)
    metadata["finished_at"] = finished.isoformat()
    output = {
        "metadata": metadata,
        "summary": summary,
        "records": records,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = finished.strftime("%Y%m%d-%H%M%S")
    stem = f"benchmark-v1-{args.suite}-{stamp}"
    json_path = RESULTS_DIR / f"{stem}.json"
    md_path = RESULTS_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str) + "\n",
                         encoding="utf-8")
    write_markdown(md_path, metadata, summary, records)

    print()
    print(f"Supported execution accuracy : {pct(summary['supported_run_execution_accuracy'])}")
    print(f"Supported semantic accuracy  : {pct(summary['supported_run_semantic_accuracy'])}")
    print(f"Supported full accuracy      : {pct(summary['supported_run_full_accuracy'])}")
    print(f"Safe rejection accuracy      : {pct(summary['safe_rejection_accuracy'])}")
    print(f"Unsafe execution rate        : {pct(summary['unsafe_execution_rate'])}")
    print(f"Stable cases                 : {summary['stable_cases']}/{summary['cases']}" if summary['stable_cases'] is not None else "Stable cases                 : n/a (one run per case)")
    print(f"Jev calls / input / output   : {summary['jev']['total_calls']} / {summary['jev']['total_input_tokens']} / {summary['jev']['total_output_tokens']}")
    print(f"Total tokens / estimated cost: {summary['jev']['total_tokens']} / ${summary['jev']['total_cost_usd']:.6f}")
    print(f"Benchmark wall-clock time    : {summary['wall_clock_s']:.2f}s")
    print(f"Reports                      : {json_path} · {md_path}")
    return 0 if all(item["full_pass"] for item in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
