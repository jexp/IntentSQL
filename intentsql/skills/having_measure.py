"""Choose which registered aggregate measure a group threshold constrains."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class HavingMeasure:
    kind: str
    status: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_having_measure(request: str, group_key: str,
                           output_measure: str, client: JevClient) -> HavingMeasure:
    criteria = {
        "output_measure": f"Threshold applies to the returned {output_measure}.",
        "row_count": "Threshold applies to the number of qualifying source rows in each group.",
        "none": "No cutoff on a computed group value. A top/bottom number of output groups is LIMIT, not a threshold on their measure.",
        "unsupported": "Threshold needs another aggregate or expression.",
    }
    state = {"request": request, "group_key": group_key,
             "returned_measure": output_measure,
             "registered_threshold_measures": ("returned measure", "row count")}
    call = client.call(state, {"measure": {
        "type": "choice",
        "instructions": "Which computed measure does the group threshold constrain?",
        "criteria": criteria,
    }})
    answer = call.answers["measure"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("System One selected an unregistered HAVING measure")
    trace = ({
        "purpose": "resolve grouped threshold measure",
        "state": state, "choices": criteria, "selected": selected,
        "probabilities": answer.get("probabilities"),
        "confidence": answer.get("confidence"),
        "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    return HavingMeasure(
        selected, "resolved" if selected in {"output_measure", "row_count", "none"}
        else "unsupported", 1, call.input_tokens, call.output_tokens, trace)
