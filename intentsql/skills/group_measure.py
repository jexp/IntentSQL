"""Resolve a grouped measure when the request asks primarily for group keys."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class GroupMeasure:
    kind: str
    qualifies_groups: bool
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_group_measure(request: str, table: str, columns: tuple[str, ...],
                          client: JevClient | None = None) -> GroupMeasure:
    """One bounded call, with schema from only the selected source table."""
    client = client or JevClient()
    choices = {
        "count": "Count source records in each group",
        "aggregate": "Compute MIN/MAX/SUM/AVG of a stored column in each group",
        "none": "No computed group measure is requested",
    }
    state = {"request": request, "selected_table": table,
             "available_columns": columns}
    call = client.call(state, {
        "measure": {"type": "choice", "instructions": "Which computed measure, if any, applies to each group?",
                    "criteria": choices},
        "qualifier": {"type": "noul", "instructions": "Does a threshold on that measure select qualifying groups?"},
    })
    kind = call.answers["measure"].get("choice")
    if kind not in choices:
        raise RuntimeError("Jev selected a grouped measure outside legal choices")
    score = float(call.answers["qualifier"]["noul"])
    trace = ({"purpose": "resolve grouped measure and threshold need",
              "state": state, "choices": choices,
              "selected": kind, "probabilities": call.answers["measure"].get("probabilities"),
              "confidence": call.answers["measure"].get("confidence"),
              "scores": {"qualifier": score},
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return GroupMeasure(kind, score >= .6, "resolved" if kind != "none" else "unsupported",
                        "jev_choice_noul", 1, call.input_tokens, call.output_tokens, trace)
