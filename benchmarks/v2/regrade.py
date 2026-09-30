#!/usr/bin/env python3
"""Re-grade a saved v2 live run after an evaluator-only correction.

This makes no IntentSQL or Jev calls. The source report is never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.v2 import run_benchmark as v2


def regrade(source: Path, destination: Path) -> dict:
    corpus = v2.validate_corpus(v2.CORPUS, verbose=False)
    original = json.loads(source.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in corpus["cases"]}
    records = original["records"]
    if len(records) != len(cases) or {row["id"] for row in records} != set(cases):
        raise ValueError("Saved run does not contain exactly the v2 corpus")
    for record in records:
        case = cases[record["id"]]
        if any(record.get(key) != case[key] for key in ("prompt", "database", "expect")):
            raise ValueError(f"Case identity changed: {case['id']}")
        result = record.get("result") if isinstance(record.get("result"), dict) else None
        _, gold_rows = v2.base.read_reference(case)
        if case["expect"] == "execute":
            semantic_pass, reasons = v2.semantic_check(
                case["semantic"], result.get("program") if result else None)
            execution_pass = bool(result and result.get("supported") is True and
                                  v2.base.rows_equivalent(result.get("rows") or [], gold_rows,
                                                          bool(case.get("result_ordered"))))
            full_pass = bool(result and result.get("supported") is True and
                             semantic_pass and execution_pass)
            record["semantic_pass"] = semantic_pass
            record["execution_pass"] = execution_pass
            record["full_pass"] = full_pass
            record["semantic_reasons"] = reasons
            record["failure_reasons"] = ([] if full_pass else
                (["semantic plan: " + "; ".join(reasons)] if reasons else []) +
                (["execution differs from independent reference SQL"] if not execution_pass else []))
        else:
            passed, basis = v2.base.rejection_pass(record)
            record["semantic_pass"] = record["execution_pass"] = record["full_pass"] = passed
            record["unsafe_execution"] = bool(result and result.get("supported") is True)
            record["rejection_basis"] = basis
            record["failure_reasons"] = [] if passed else [basis]
    metadata = dict(original["metadata"])
    metadata["corpus_sha256"] = v2.base.corpus_hash(v2.CORPUS)
    metadata["source_report_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata["regraded_at"] = datetime.now(timezone.utc).isoformat()
    metadata["evaluation_note"] = (
        "REJ-07 joined filter ownership is stored as separate table and column fields; "
        "all original live results and usage are preserved."
    )
    summary = v2.base.summarize(records, metadata["runs_per_case"],
                                original["summary"]["wall_clock_s"])
    report = {"metadata": metadata, "summary": summary, "records": records}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
        handle.write("\n")
    v2.base.write_markdown(destination.with_suffix(".md"), metadata, summary, records)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="original v2 live JSON report")
    parser.add_argument("--output", type=Path, help="new derived JSON report")
    args = parser.parse_args()
    target = args.output or args.source.with_name(args.source.stem + "-regraded.json")
    summary = regrade(args.source, target)
    print(f"Regraded {summary['runs']} saved cases; no Jev calls. Report: {target}")
    print("Supported full accuracy:", summary["supported_run_full_accuracy"])


if __name__ == "__main__":
    main()
