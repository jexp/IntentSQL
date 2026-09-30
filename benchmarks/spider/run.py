#!/usr/bin/env python3
"""Run a frozen Spider 1.0 subset against the real IntentSQL API."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import sys
import time
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.spider.prepare import (ARCHIVE, MANIFEST, SECOND_MANIFEST,
                                       THIRD_MANIFEST, FOURTH_MANIFEST, FIFTH_MANIFEST,
                                       SIXTH_MANIFEST, SEVENTH_MANIFEST,
                                       file_digest, preflight)  # noqa: E402
from benchmarks.v1.run_benchmark import (http_json, percentile, rows_equivalent,  # noqa: E402
                                          run_sse, workspace_root)
from intentsql.database import connect, readonly_uri  # noqa: E402
from benchmarks.artifacts import artifact_root  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS = artifact_root() / "spider/results"
MAX_ROWS = 5000
AGGREGATES = {0: "", 1: "MAX", 2: "MIN", 3: "COUNT", 4: "SUM", 5: "AVG"}
OPERATORS = {2: "=", 3: ">", 4: "<", 5: ">=", 6: "<=", 7: "!="}


def architecture_digest() -> str:
    """Hash source content identically across LF and CRLF checkouts."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "intentsql").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def save_progress(path: Path, output: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(output, indent=2, ensure_ascii=False,
                                    default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_resume(saved: dict, identity: dict, cases: list[dict]) -> None:
    if any(saved.get(key) != value for key, value in identity.items()):
        raise ValueError("Checkpoint source, manifest, or provider changed; cannot resume")
    if saved.get("in_flight"):
        raise ValueError("An interrupted case may have spent Jev calls; recover its output before resuming")
    ids = [record["id"] for record in saved["records"]]
    if ids != [case["id"] for case in cases[:len(ids)]] or len(ids) > len(cases):
        raise ValueError("Checkpoint is not an exact prefix of the frozen selection")


def expected_semantics(case: dict, metadata: dict) -> dict:
    sql = case["gold_ast"]
    table_id = sql["from"]["table_units"][0][1]
    names = metadata["column_names_original"]

    def column(unit: list) -> str:
        return names[unit[1]][1]

    agg_id, value_unit = sql["select"][1][0]
    selected = column(value_unit[1])
    output = f"{AGGREGATES[agg_id]}({selected})" if agg_id else selected
    expected = {
        "table": metadata["table_names_original"][table_id],
        "output": output, "distinct": bool(sql["select"][0]),
        "limit": sql["limit"], "filter": None, "order": None,
    }
    if sql["where"]:
        item = sql["where"][0]
        value = item[3]
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1].replace(value[0] * 2, value[0])
        expected["filter"] = {"column": column(item[2][1]),
                              "operator": OPERATORS[item[1]], "value": value}
    if sql["orderBy"]:
        direction, values = sql["orderBy"]
        expected["order"] = {"key": column(values[0][1]),
                             "direction": direction.upper()}
    return expected


def same_value(a: object, b: object) -> bool:
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except (InvalidOperation, TypeError):
            return False
    return str(a).casefold() == str(b).casefold()


def semantic_check(case: dict, metadata: dict, program: dict | None) -> tuple[bool, list[str]]:
    if not isinstance(program, dict):
        return False, ["No typed program"]
    gold = expected_semantics(case, metadata)
    reasons = []
    if str(program.get("base_table", "")).casefold() != gold["table"].casefold():
        reasons.append("source table differs")
    if len(program.get("joined_tables") or []) != 1 or program.get("joins"):
        reasons.append("unexpected join")
    outputs = program.get("outputs") or []
    typed = program.get("typed_query") or {}
    # The compiler's display label for SELECT * is "all columns". Use the
    # typed shape rather than treating that UI label as a selected identifier.
    all_columns = (gold["output"] == "*" and typed.get("output") == "rows"
                   and not typed.get("columns") and not typed.get("relation"))
    if not all_columns and (len(outputs) != 1 or str(outputs[0]).casefold() != gold["output"].casefold()):
        reasons.append("selected output differs")
    if program.get("groups") or program.get("having"):
        reasons.append("unexpected grouping or having")
    if program.get("limit") != gold["limit"]:
        reasons.append("limit differs")
    if bool((program.get("typed_query") or {}).get("distinct")) != gold["distinct"]:
        reasons.append("distinct differs")
    filters = program.get("filters") or []
    wanted = gold["filter"]
    if wanted is None:
        if filters:
            reasons.append("unrequested filter")
    elif (len(filters) != 1 or
          str(filters[0].get("column", "")).casefold() != wanted["column"].casefold() or
          filters[0].get("operator") != wanted["operator"] or
          not same_value(filters[0].get("value"), wanted["value"])):
        reasons.append("filter differs")
    order = program.get("order_by") or []
    wanted_order = gold["order"]
    if wanted_order is None:
        if order:
            reasons.append("unrequested ordering")
    elif (len(order) != 1 or
          str(order[0].get("key", "")).casefold() != wanted_order["key"].casefold() or
          str(order[0].get("direction", "")).upper() != wanted_order["direction"]):
        reasons.append("ordering differs")
    return not reasons, reasons


def gold_rows(path: Path, sql: str) -> list[list]:
    with connect(readonly_uri(path), uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = [list(row) for row in conn.execute(sql).fetchmany(MAX_ROWS + 1)]
    if len(rows) > MAX_ROWS:
        raise ValueError(f"Gold query exceeds {MAX_ROWS} comparable rows")
    return rows


def extract_selected(archive: Path, frozen: dict) -> dict[str, Path]:
    destination = artifact_root() / "spider/databases"
    destination.mkdir(parents=True, exist_ok=True)
    paths = {}
    with zipfile.ZipFile(archive) as source:
        for case in frozen["cases"]:
            db_id = case["db_id"]
            if db_id in paths:
                continue
            member = f"spider_data/database/{db_id}/{db_id}.sqlite"
            path = destination / f"{db_id}.sqlite"
            path.write_bytes(source.read(member))
            paths[db_id] = path
    for case in frozen["cases"]:
        if file_digest(paths[case["db_id"]]) != case["database_sha256"]:
            raise ValueError(f"Database hash mismatch: {case['id']}")
    return paths


def preflight_gold(frozen: dict, paths: dict[str, Path]) -> tuple[dict, dict]:
    with zipfile.ZipFile(ARCHIVE) as source:
        metadata = {item["db_id"]: item for item in json.loads(source.read("spider_data/tables.json"))}
    references = {}
    for case in frozen["cases"]:
        references[case["id"]] = gold_rows(paths[case["db_id"]], case["gold_sql"])
        expected_semantics(case, metadata[case["db_id"]])
    print(f"Gold SQLite preflight passed for {len(references)} cases; no IntentSQL execution.")
    return references, metadata


def summarize(records: list[dict], wall_time: float) -> dict:
    calls = [int(r["stats"].get("jev_calls") or 0) for r in records]
    ins = [int(r["stats"].get("input_tokens") or 0) for r in records]
    outs = [int(r["stats"].get("output_tokens") or 0) for r in records]
    costs = [float(r["stats"].get("cost_usd") or 0) for r in records]
    latencies = [r["latency_s"] for r in records]
    return {
        "passed": sum(r["passed"] for r in records), "total": len(records),
        "execution_accuracy": sum(r["execution_pass"] for r in records) / len(records),
        "semantic_accuracy": sum(r["semantic_pass"] for r in records) / len(records),
        "total_calls": sum(calls), "input_tokens": sum(ins),
        "output_tokens": sum(outs), "total_tokens": sum(ins) + sum(outs),
        "estimated_cost_usd": sum(costs),
        "median_latency_s": statistics.median(latencies),
        "p95_latency_s": percentile(latencies, .95), "wall_time_s": wall_time,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:7862")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--sample", type=int, choices=(1, 2, 3, 4, 5, 6, 7), default=1)
    parser.add_argument("--regression", action="store_true",
                        help="retest a frozen development case set without overwriting its first-run report")
    parser.add_argument("--resume", action="store_true",
                        help="resume a checkpoint without repeating completed cases")
    args = parser.parse_args()
    manifest = {1: MANIFEST, 2: SECOND_MANIFEST,
                3: THIRD_MANIFEST, 4: FOURTH_MANIFEST,
                5: FIFTH_MANIFEST, 6: SIXTH_MANIFEST,
                7: SEVENTH_MANIFEST}[args.sample]
    stem = {1: "spider-20", 2: "spider-20-b",
            3: "spider-final-20-a", 4: "spider-final-20-b",
            5: "spider-decision-tree-20", 6: "spider-release-20",
            7: "spider-final-unseen-40"}[args.sample]
    report = RESULTS / (stem + ("-regression" if args.regression else "") + ".json")
    checkpoint = report.with_suffix(".partial.json")
    if not args.preflight_only and report.exists():
        raise RuntimeError("Spider live report already exists; this selection may run only once")
    frozen = preflight(ARCHIVE, manifest, args.sample)
    paths = extract_selected(ARCHIVE, frozen)
    references, metadata = preflight_gold(frozen, paths)
    if args.preflight_only:
        return 0
    health = http_json(args.url, "/api/health")
    config = http_json(args.url, "/api/connection")
    if not health.get("ready") or (config.get("requires_key") and not config.get("has_key")):
        raise SystemExit("IntentSQL server/provider is not ready")
    RESULTS.mkdir(parents=True, exist_ok=True)
    identity = {"source_archive_sha256": frozen["archive_sha256"], "sample": args.sample,
                "run_kind": "development_regression" if args.regression else "external_frozen",
                "selection_seed": frozen["seed"], "provider": config.get("name"),
                "model": config.get("model"), "endpoint": config.get("url"),
                "evaluator_sha256": file_digest(Path(__file__)),
                "architecture_sha256": architecture_digest(), "manifest_sha256": file_digest(manifest)}
    # A regression run intentionally evaluates a newer architecture against an
    # already-frozen development set. Architecture locks protect held-out first
    # runs only; applying them here would make --regression unusable.
    if args.sample in (3, 4, 5, 6, 7) and not args.regression:
        lock = RESULTS / ("final-unseen-architecture.json" if args.sample == 7 else
                          "release-architecture.json" if args.sample == 6 else
                          "decision-tree-architecture.json" if args.sample == 5 else
                          "heldout-architecture.json")
        frozen_architecture = {key: identity[key] for key in
                               ("architecture_sha256", "evaluator_sha256", "provider", "model", "endpoint")}
        if lock.exists():
            if json.loads(lock.read_text(encoding="utf-8")) != frozen_architecture:
                raise ValueError("Held-out architecture/provider changed between samples")
        else:
            with lock.open("x", encoding="utf-8") as handle:
                json.dump(frozen_architecture, handle, indent=2)
    output = {**identity, "records": [], "in_flight": None, "wall_time_s": 0.0}
    if checkpoint.exists():
        if not args.resume:
            raise ValueError("Checkpoint exists; use --resume to preserve completed calls")
        output = json.loads(checkpoint.read_text(encoding="utf-8"))
        validate_resume(output, identity, frozen["cases"])
    elif args.resume:
        raise ValueError("No checkpoint to resume")
    runtime_dir = workspace_root() / "databases"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime = {}
    for db_id, path in paths.items():
        target = runtime_dir / f"intentspider_{db_id}_{int(time.time())}.db"
        shutil.copy2(path, target)
        runtime[db_id] = target
    records = output["records"]
    prior_wall = output["wall_time_s"]
    started = time.perf_counter()
    try:
        for case in frozen["cases"][len(records):]:
            output["in_flight"] = case["id"]
            save_progress(checkpoint, output)
            run = run_sse(args.url, runtime[case["db_id"]].name, case["question"], args.timeout)
            result = run.get("result") if isinstance(run.get("result"), dict) else None
            supported = bool(result and result.get("supported") is True)
            semantic_pass, reasons = semantic_check(case, metadata[case["db_id"]],
                                                     result.get("program") if supported else None)
            execution_pass = bool(supported and rows_equivalent(result.get("rows") or [],
                                   references[case["id"]], bool(case["gold_ast"]["orderBy"])))
            record = {
                "id": case["id"], "db_id": case["db_id"],
                "question": case["question"], "gold_sql": case["gold_sql"],
                "generated_sql": result.get("sql") if result else None,
                "generated_params": result.get("params") if result else None,
                "program": result.get("program") if result else None,
                "gold_row_count": len(references[case["id"]]),
                "actual_row_count": len(result.get("rows") or []) if supported else None,
                "gold_sample": references[case["id"]][:5],
                "actual_sample": (result.get("rows") or [])[:5] if supported else None,
                "rejected": not supported, "error": run.get("error"),
                "semantic_pass": semantic_pass, "semantic_reasons": reasons,
                "execution_pass": execution_pass,
                "passed": semantic_pass and execution_pass,
                "latency_s": run["elapsed_s"], "stats": run.get("stats") or {},
                "events": run.get("events") or [],
            }
            records.append(record)
            output["in_flight"] = None
            output["wall_time_s"] = prior_wall + time.perf_counter() - started
            save_progress(checkpoint, output)
            print(f"{'PASS' if record['passed'] else 'FAIL'} {case['id']} {case['db_id']} "
                  f"{record['latency_s']:.2f}s", flush=True)
    finally:
        for path in runtime.values():
            path.unlink(missing_ok=True)
    summary = summarize(records, output["wall_time_s"])
    output["summary"] = summary
    with report.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(output, indent=2, ensure_ascii=False, default=str) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"Report: {report}")
    return 0 if summary["passed"] == len(frozen["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
