"""Resolve ordering of grouped results by key or computed measure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class GroupOrdering:
    target: str
    direction: str
    tie_key_direction: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    secondary_target: str | None = None
    secondary_direction: str | None = None


def resolve_group_ordering(request: str, group_column: str, measure: str,
                           client: JevClient | None = None) -> GroupOrdering:
    client = client or JevClient()
    state = {"request": request, "group_key": group_column, "aggregate_measure": measure}
    questions = {
        "target": {"type": "choice", "instructions": "Is the primary ranking by the group key or computed aggregate measure?",
                   "criteria": {"key": group_column, "measure": measure}},
        "direction": {"type": "choice", "instructions": "Primary sort direction?",
                      "criteria": {"ASC": "smallest/earliest/A-Z first",
                                   "DESC": "largest/latest/Z-A first"}},
        "tie": {"type": "choice", "instructions": "Is there a second sort term after the primary grouped ordering?",
                "criteria": {"none": "no second grouped sort term",
                             "ASC": "group key ascending (legacy key tie choice)",
                             "DESC": "group key descending (legacy key tie choice)",
                             "key_ASC": "group key ascending",
                             "key_DESC": "group key descending",
                             "measure_ASC": "computed measure ascending",
                             "measure_DESC": "computed measure descending"}},
    }
    call = client.call(state, questions)
    selected = {key: call.answers[key].get("choice") for key in questions}
    if (selected["target"] not in ("key", "measure") or
        selected["direction"] not in ("ASC", "DESC") or
        selected["tie"] not in ("none", "ASC", "DESC", "key_ASC", "key_DESC",
                                 "measure_ASC", "measure_DESC")):
        raise RuntimeError("Jev selected invalid grouped ordering")
    secondary = None if selected["tie"] == "none" else selected["tie"]
    if secondary in ("ASC", "DESC"):
        secondary = "key_" + secondary
    secondary_target = secondary.split("_")[0] if secondary else None
    secondary_direction = secondary.split("_")[1] if secondary else None
    trace = ({"purpose": "resolve grouped ordering", "state": state,
              "choices": {key: value["criteria"] for key, value in questions.items()},
              "selected": selected,
              "probabilities": {key: call.answers[key].get("probabilities") for key in questions},
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return GroupOrdering(selected["target"], selected["direction"],
                         secondary_direction if secondary_target == "key" else None,
                         "resolved", "jev_choice", 1,
                         call.input_tokens, call.output_tokens, trace,
                         secondary_target, secondary_direction)
