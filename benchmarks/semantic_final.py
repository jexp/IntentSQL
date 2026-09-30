"""One frozen targeted validation pass; no development retries or tuning."""
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.spider.prepare import ARCHIVE, file_digest
from benchmarks.spider.run import architecture_digest, gold_rows, semantic_check, summarize
from benchmarks.v1.run_benchmark import http_json, rows_equivalent, workspace_root
from benchmarks.artifacts import artifact_root
from intentsql.database import connect
from tools.e2e import run_sse

PLAN = ROOT / "benchmarks/semantic-final-plan.json"
REPORT = artifact_root() / "semantic/results/semantic-final-targeted.json"


def main():
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if architecture_digest() != plan["architecture_sha256"]:
        raise RuntimeError("Architecture changed after freeze")
    partial = REPORT.with_suffix(".partial.json")
    if REPORT.exists() or partial.exists():
        raise RuntimeError("Targeted run already started; refuses duplicate provider work")
    base = plan["url"]
    config = http_json(base, "/api/connection")
    if config.get("requires_key") and not config.get("has_key"):
        raise RuntimeError("Provider key is not configured")
    directory = workspace_root() / "databases"
    directory.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    records, created = [], []
    output = {"plan_sha256": file_digest(PLAN), "architecture_sha256": architecture_digest(),
              "records": records, "in_flight": None}

    def save():
        temporary = partial.with_suffix(".tmp")
        temporary.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
        temporary.replace(partial)

    started = time.perf_counter()
    try:
        with zipfile.ZipFile(ARCHIVE) as archive:
            metadata = {item["db_id"]: item for item in json.loads(archive.read("spider_data/tables.json"))}
            for index, case in enumerate(plan["targeted_cases"]):
                path = directory / f"semantic_final_{index}.db"
                if path.exists():
                    raise RuntimeError(f"Validation fixture already exists: {path.name}")
                created.append(path)
                if case["kind"] == "spider":
                    db = case["db_id"]
                    path.write_bytes(archive.read(f"spider_data/database/{db}/{db}.sqlite"))
                elif case["kind"] == "mutation":
                    with connect(path) as conn:
                        conn.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY, name TEXT, stock INTEGER)")
                        conn.executemany("INSERT INTO inventory VALUES (?,?,?)", [(1, "Alpha", 8), (2, "Beta", 4)])
                else:
                    shutil.copy2(ROOT / "data" / case["database"], path)
                expected = gold_rows(path, case["gold_sql"]) if case.get("gold_sql") else None
                output["in_flight"] = case["id"]
                save()
                run = run_sse(base, path.name, case["question"],
                              "auto" if case["kind"] == "mutation" else "read", 120)
                result = run.get("result") or {}
                supported = result.get("supported") is True
                execution = semantic = False
                reasons = []
                if case["kind"] == "mutation":
                    with connect(path) as conn:
                        before = [list(row) for row in conn.execute("SELECT * FROM inventory ORDER BY id")]
                    semantic = bool(supported and result.get("affected") == 1
                                    and result.get("program", {}).get("operation") == case["operation"])
                    if supported and result.get("affected") == 1:
                        token = result["commit_token"]
                        committed = http_json(base, "/api/commit", method="POST", body={"token": token, "confirm": True})
                        with connect(path) as conn:
                            changed = [list(row) for row in conn.execute("SELECT * FROM inventory ORDER BY id")]
                        http_json(base, "/api/undo", method="POST", body={"token": committed["undo_token"]})
                        with connect(path) as conn:
                            restored = [list(row) for row in conn.execute("SELECT * FROM inventory ORDER BY id")]
                        execution = (before == [[1, "Alpha", 8], [2, "Beta", 4]]
                                     and changed == case["expected_after"] and restored == before)
                elif case["kind"] == "reject":
                    execution = semantic = bool(not supported and run.get("error") and
                        not any(event.get("kind") == "compiled" for event in run["events"]) and
                        not (run.get("stats") or {}).get("failed_calls") and
                        (run.get("stats") or {}).get("jev_calls", 0) > 0)
                else:
                    execution = bool(supported and rows_equivalent(result.get("rows", []), expected, case.get("ordered", False)))
                    if case["kind"] == "spider":
                        semantic, reasons = semantic_check(case, metadata[case["db_id"]], result.get("program"))
                    else:
                        typed = (result.get("program") or {}).get("typed_query") or {}
                        semantic = bool(supported and all(typed.get(k) == v for k, v in case["expected_typed"].items()))
                record = {"id": case["id"], "class": case["class"], "passed": semantic and execution,
                          "execution_pass": execution, "semantic_pass": semantic, "semantic_reasons": reasons,
                          "rejected": not supported, "latency_s": run["elapsed_s"], "stats": run.get("stats") or {},
                          "error": run.get("error"), "result": result, "events": run["events"]}
                records.append(record)
                output["in_flight"] = None
                save()
                print(f"{'PASS' if record['passed'] else 'FAIL'} {case['id']} {case['class']}", flush=True)
    finally:
        for path in created:
            path.unlink(missing_ok=True)
    output["summary"] = summarize(records, time.perf_counter() - started)
    with REPORT.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, default=str)
    print(json.dumps(output["summary"], indent=2))
    return 0 if all(record["passed"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
