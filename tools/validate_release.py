#!/usr/bin/env python3
"""
Run the IntentSQL release-validation suites from one command.

Suites
------
offline   - unittest discovery
alpha     - frozen Alpha gate (benchmarks/alpha_gate.py)
crud      - live CRUD matrix
core      - frozen Core suite through benchmarks/development.py
cs50      - alpha-applicable CS50 cases through benchmarks/development.py
semantic  - frozen semantic regression cases through benchmarks/development.py
spider    - the six previously-used Spider development samples

The script:
- uses the current Python interpreter / venv;
- reuses provider settings from the environment, or from IntentSQL's saved
  connection settings when available;
- writes one output JSON per suite plus one combined summary;
- streams suite output to the terminal and saves logs;
- does not automatically rerun a failed suite, avoiding duplicate provider work;
- treats provider/infrastructure errors as INCONCLUSIVE rather than semantic FAIL;
- treats Spider as diagnostic by default because it is a seen development set.
  Pass --strict-spider if you want Spider failures to block the overall gate.

Run from the repository root:
    python tools/validate_release.py

Selected suites:
    python tools/validate_release.py --only offline,alpha,crud,core

Make Spider blocking:
    python tools/validate_release.py --strict-spider
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:7862"
ALL_SUITES = ("offline", "alpha", "crud", "core", "cs50", "semantic", "spider")
STRICT_DEFAULT = {"offline", "alpha", "crud", "core", "cs50", "semantic"}

INFRA_MARKERS = (
    "http 408",
    "http 425",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "http 520",
    "http 521",
    "http 522",
    "http 523",
    "http 524",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "overloaded",
    "service unavailable",
    "connection reset",
    "connection refused",
    "remote end closed",
    "provider",
)


def repo_root() -> Path:
    candidates = [Path.cwd(), Path(__file__).resolve().parent]
    candidates += list(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (
            (candidate / "intentsql").is_dir()
            and (candidate / "benchmarks").is_dir()
            and (candidate / "tests").is_dir()
        ):
            return candidate
    raise SystemExit("Run this script from the IntentSQL repository root.")


def git_info(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return proc.stdout.strip()

    return {
        "head": run("rev-parse", "--short", "HEAD"),
        "branch": run("branch", "--show-current"),
        "status": run("status", "--short"),
    }


def load_provider_env(root: Path) -> tuple[dict[str, str], str]:
    env = os.environ.copy()

    if env.get("SYSTEM_ONE_API_KEY"):
        return env, "environment"

    # Reuse the same saved IntentSQL connection config without printing secrets.
    sys.path.insert(0, str(root))
    try:
        from intentsql.web import connection_settings  # type: ignore

        cfg = connection_settings()
        key = cfg.get("api_key")
        if key:
            env["SYSTEM_ONE_API_KEY"] = str(key)
            if cfg.get("url"):
                env["SYSTEM_ONE_URL"] = str(cfg["url"])
            if cfg.get("model"):
                env["SYSTEM_ONE_MODEL"] = str(cfg["model"])
            return env, "saved IntentSQL connection"
    except Exception:
        pass
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass

    return env, "not detected"


def http_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def check_server(base_url: str) -> dict[str, Any]:
    try:
        health = http_json(base_url.rstrip("/") + "/api/health")
        return {"reachable": True, "health": health}
    except Exception as exc:
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def looks_infrastructure(text: str) -> bool:
    folded = text.casefold()
    return any(marker in folded for marker in INFRA_MARKERS)


def stream_command(
    name: str,
    command: list[str],
    root: Path,
    env: dict[str, str],
    log_path: Path,
) -> tuple[int, str, float]:
    print("\n" + "=" * 100)
    print(f"{name.upper()}")
    print("$ " + " ".join(command))
    print("=" * 100, flush=True)

    started = time.perf_counter()
    chunks: list[str] = []

    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert proc.stdout is not None

        for line in proc.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
            chunks.append(line)

        rc = proc.wait()

    elapsed = time.perf_counter() - started
    text = "".join(chunks)
    print(f"\n[{name}] exit={rc} elapsed={elapsed:.1f}s")
    return rc, text, elapsed


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def classify_alpha(report: dict[str, Any] | None, rc: int, log: str) -> tuple[str, dict[str, Any]]:
    if not report:
        return ("INCONCLUSIVE" if looks_infrastructure(log) else "ERROR",
                {"reason": "alpha output JSON missing/unreadable"})

    summary = report.get("summary") or {}
    fail = int(summary.get("FAIL", 0) or 0)
    provider = int(summary.get("PROVIDER", 0) or 0)
    passed = int(summary.get("PASS", 0) or 0)
    cases = int(summary.get("cases", passed + fail + provider) or 0)

    if provider:
        status = "INCONCLUSIVE"
    elif fail:
        status = "FAIL"
    elif cases and passed == cases:
        status = "PASS"
    else:
        status = "ERROR" if rc else "PASS"

    return status, summary


def classify_crud(report: dict[str, Any] | None, rc: int, log: str) -> tuple[str, dict[str, Any]]:
    if not report:
        return ("INCONCLUSIVE" if looks_infrastructure(log) else "ERROR",
                {"reason": "CRUD output JSON missing/unreadable"})

    passed = int(report.get("passed", 0) or 0)
    total = int(report.get("total", 0) or 0)
    failures = [row for row in report.get("results", []) if not row.get("passed")]

    infra = [
        row for row in failures
        if looks_infrastructure(str(row.get("error") or ""))
    ]

    if infra:
        status = "INCONCLUSIVE"
    elif total and passed == total:
        status = "PASS"
    else:
        status = "FAIL"

    return status, {
        "passed": passed,
        "total": total,
        "calls": report.get("calls"),
        "input_tokens": report.get("input_tokens"),
        "output_tokens": report.get("output_tokens"),
        "median_latency": report.get("median_latency"),
        "infrastructure_failures": len(infra),
    }


def classify_development(
    report: dict[str, Any] | None,
    rc: int,
    log: str,
) -> tuple[str, dict[str, Any]]:
    if not report:
        return ("INCONCLUSIVE" if looks_infrastructure(log) else "ERROR",
                {"reason": "development output JSON missing/unreadable"})

    summary = report.get("summary") or {}
    total = int(summary.get("total", 0) or 0)
    passed = int(summary.get("passed", 0) or 0)
    infra = int(summary.get("infrastructure_errors", 0) or 0)

    if infra:
        status = "INCONCLUSIVE"
    elif total and passed == total:
        status = "PASS"
    else:
        status = "FAIL"

    return status, summary


def summarize_failure_ids(report: dict[str, Any] | None) -> list[str]:
    if not report:
        return []
    rows = report.get("records") or report.get("results") or []
    out = []
    for row in rows:
        if row.get("passed") is False:
            value = row.get("id") or row.get("class")
            if value:
                out.append(str(value))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        help="comma-separated subset: " + ",".join(ALL_SUITES),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="IntentSQL server URL used by the alpha gate",
    )
    parser.add_argument(
        "--strict-spider",
        action="store_true",
        help="make seen Spider development failures block the overall gate",
    )
    parser.add_argument(
        "--between-suites",
        type=float,
        default=1.0,
        help="seconds to wait between suites (default: 1)",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        help="explicit output directory; default is benchmarks/results/release-validation-<timestamp>",
    )
    parser.add_argument(
        "--skip-server-check",
        action="store_true",
        help="skip the preflight health check for the alpha HTTP server",
    )
    args = parser.parse_args()

    root = repo_root()
    selected = list(ALL_SUITES)

    if args.only:
        requested = [x.strip().lower() for x in args.only.split(",") if x.strip()]
        unknown = sorted(set(requested) - set(ALL_SUITES))
        if unknown:
            parser.error("unknown suite(s): " + ", ".join(unknown))
        selected = requested

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sys.path.insert(0, str(root))
    from benchmarks.artifacts import artifact_root
    results_dir = args.results_dir or (artifact_root() / "release" / f"release-validation-{stamp}")
    results_dir.mkdir(parents=True, exist_ok=False)

    git = git_info(root)
    env, provider_source = load_provider_env(root)

    server = None
    if "alpha" in selected and not args.skip_server_check:
        server = check_server(args.base_url)
        if not server["reachable"]:
            print(json.dumps(server, indent=2))
            raise SystemExit(
                "Alpha gate selected but IntentSQL server is not reachable. "
                "Start Uvicorn or use --only without alpha."
            )

    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(root),
        "python": sys.executable,
        "python_version": sys.version,
        "git": git,
        "provider_config_source": provider_source,
        "base_url": args.base_url,
        "server": server,
        "selected_suites": selected,
        "strict_spider": args.strict_spider,
    }

    combined: dict[str, Any] = {
        "metadata": metadata,
        "suites": {},
        "overall": {},
    }

    combined_path = results_dir / "combined-summary.json"

    def checkpoint() -> None:
        combined_path.write_text(
            json.dumps(combined, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )

    checkpoint()

    print(f"Repo: {root}")
    print(f"Python: {sys.executable}")
    print(f"Git HEAD: {git['head']} ({git['branch']})")
    print(f"Provider config: {provider_source}")
    print(f"Results: {results_dir}")
    if git["status"]:
        print("\nWARNING: working tree is not clean:")
        print(git["status"])
    print("\nSuites:", ", ".join(selected))

    for index, suite in enumerate(selected):
        out_json: Path | None = None

        if suite == "offline":
            command = [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test*.py",
            ]

        elif suite == "alpha":
            alpha_script = root / "benchmarks" / "alpha_gate.py"
            if not alpha_script.exists():
                raise SystemExit(f"Missing {alpha_script.name}")
            out_json = results_dir / "alpha.json"
            command = [
                sys.executable,
                str(alpha_script),
                "--base-url",
                args.base_url,
                "--out",
                str(out_json),
            ]

        elif suite == "crud":
            out_json = results_dir / "crud.json"
            command = [
                sys.executable,
                "benchmarks/crud_matrix.py",
                "--output",
                str(out_json),
                "--trace",
            ]

        else:
            out_json = results_dir / f"{suite}.json"
            command = [
                sys.executable,
                "benchmarks/development.py",
                "--suite",
                suite,
                "--output",
                str(out_json),
            ]

        log_path = results_dir / f"{suite}.log"
        rc, log, elapsed = stream_command(
            suite, command, root, env, log_path
        )

        report = read_json(out_json) if out_json else None

        if suite == "offline":
            if rc == 0:
                status = "PASS"
            elif looks_infrastructure(log):
                status = "INCONCLUSIVE"
            else:
                status = "FAIL"
            details = {
                "returncode": rc,
                "tail": "\n".join(log.splitlines()[-20:]),
            }

        elif suite == "alpha":
            status, details = classify_alpha(report, rc, log)

        elif suite == "crud":
            status, details = classify_crud(report, rc, log)

        else:
            status, details = classify_development(report, rc, log)

        combined["suites"][suite] = {
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
            "returncode": rc,
            "output_json": str(out_json) if out_json else None,
            "log": str(log_path),
            "summary": details,
            "failure_ids": summarize_failure_ids(report),
        }
        checkpoint()

        print(f"\n>>> {suite.upper()}: {status}")
        if combined["suites"][suite]["failure_ids"]:
            print("    failures:", ", ".join(combined["suites"][suite]["failure_ids"]))

        if index < len(selected) - 1 and args.between_suites > 0:
            time.sleep(args.between_suites)

    strict = set(STRICT_DEFAULT)
    if args.strict_spider:
        strict.add("spider")
    strict &= set(selected)

    statuses = {name: combined["suites"][name]["status"] for name in selected}
    strict_statuses = {name: statuses[name] for name in strict}

    if any(value == "FAIL" for value in strict_statuses.values()):
        gate = "NOT_READY"
    elif any(value in ("INCONCLUSIVE", "ERROR") for value in strict_statuses.values()):
        gate = "INCONCLUSIVE"
    elif all(value == "PASS" for value in strict_statuses.values()):
        spider = statuses.get("spider")
        if spider and spider != "PASS" and not args.strict_spider:
            gate = "CORE_GATES_PASS_WITH_SPIDER_DEVELOPMENT_GAPS"
        else:
            gate = "PASS"
    else:
        gate = "PARTIAL"

    combined["overall"] = {
        "gate": gate,
        "strict_suites": sorted(strict),
        "statuses": statuses,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    checkpoint()

    print("\n" + "=" * 100)
    print("FINAL VALIDATION SUMMARY")
    print("=" * 100)
    for suite in selected:
        row = combined["suites"][suite]
        summary = row["summary"]
        short = ""
        if "passed" in summary and "total" in summary:
            short = f" {summary['passed']}/{summary['total']}"
        elif "PASS" in summary and "cases" in summary:
            short = f" {summary.get('PASS', 0)}/{summary.get('cases', 0)}"
        print(f"{suite:10} {row['status']:14}{short}")

    print(f"\nOVERALL: {gate}")
    print(f"Combined JSON: {combined_path}")
    print(f"All outputs/logs: {results_dir}")

    if gate in ("NOT_READY", "INCONCLUSIVE"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
