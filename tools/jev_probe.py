#!/usr/bin/env python3
"""Run repeatable, small System One experiments from a JSON fixture.

Fixture format: {"cases": [{"name": ..., "state": ..., "questions": ...,
"expected": {"question_id": "choice_id"}}]}. The fixture must contain no secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from intentsql.jev_client import JevClient  # noqa: E402


def selected(answer: dict[str, Any]) -> Any:
    return answer.get("choice", answer.get("noul", answer.get("score")))


def run_fixture(fixture: dict[str, Any], repeat: int = 1,
                client: JevClient | None = None) -> dict[str, Any]:
    client = client or JevClient()
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Fixture needs a nonempty cases list")
    runs = []
    for case in cases:
        name = case["name"]
        state = case["state"]
        questions = case["questions"]
        expected = case.get("expected", {})
        for iteration in range(repeat):
            call = client.call(state, questions)
            answers = call.answers
            picks = {key: selected(value) for key, value in answers.items()}
            correct = {key: picks.get(key) == value for key, value in expected.items()}
            runs.append({
                "name": name, "iteration": iteration + 1, "purpose": case.get("purpose", name),
                "selected": picks, "correct": correct,
                "answers": answers, "usage": {
                    "calls": 1, "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens, "elapsed_ms": call.elapsed_ms,
                },
            })
    totals = {
        "calls": len(runs),
        "input_tokens": sum(run["usage"]["input_tokens"] for run in runs),
        "output_tokens": sum(run["usage"]["output_tokens"] for run in runs),
        "correct": sum(sum(run["correct"].values()) for run in runs),
        "checks": sum(len(run["correct"]) for run in runs),
    }
    return {"created_at": datetime.now(timezone.utc).isoformat(),
            "suite": fixture.get("suite", "probe"), "repeat": repeat,
            "runs": runs, "totals": totals}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--save", type=Path, help="Save responses/usage as JSON; never includes API key")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    result = run_fixture(fixture, args.repeat)
    for run in result["runs"]:
        usage = run["usage"]
        print(f"{run['name']} #{run['iteration']}: {run['selected']} "
              f"correct={run['correct']} tokens={usage['input_tokens']}+"
              f"{usage['output_tokens']} time={usage['elapsed_ms']}ms")
        for key, answer in run["answers"].items():
            print(f"  {key}: confidence={answer.get('confidence')} "
                  f"probabilities={answer.get('probabilities')}")
    print(json.dumps(result["totals"], sort_keys=True))
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
