"""Disambiguate scalar count, distinct count, and grouped count."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class CountShape:
    shape: str
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_count_shape(request: str, columns: tuple[str, ...],
                        client: JevClient | None = None) -> CountShape:
    client = client or JevClient()
    choices = {
        "scalar": "one number counting all qualifying source rows, including rows matching several alternative filter values",
        "distinct": "one number counting distinct values of one source column",
        "grouped": "one row per group with a count for each group",
    }
    state = {"request": request, "candidate_columns": columns,
             "task": "Choose the result shape before resolving count targets. Different/unique values counted into one number are distinct, not groups. Several allowed filter values do not imply a separate answer per value."}
    call = client.call(state, {"shape": {"type": "choice",
        "instructions": "Is this a scalar count, a scalar distinct count, or a grouped count?",
        "criteria": choices}})
    answer = call.answers["shape"]
    selected = answer.get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected an invalid count result shape")
    trace = ({"purpose": "resolve count result shape", "state": state,
              "choices": choices, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    # The dedicated shape decision is authoritative; reusing the router's earlier
    # grouping guess here biases a second call toward the interpretation being checked.
    return CountShape(selected, "resolved", "jev_choice", 1,
                      call.input_tokens, call.output_tokens, trace)
