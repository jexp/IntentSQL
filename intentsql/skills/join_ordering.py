"""Resolve one ORDER BY field and direction from a declared joined pair."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.join_fields import QualifiedField
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class JoinOrdering:
    field: QualifiedField | None
    direction: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_join_ordering(request: str, table_columns: dict[str, tuple[Column, ...]],
                          client: JevClient | None = None) -> JoinOrdering:
    fields = [QualifiedField(table, column.name)
              for table, columns in table_columns.items() for column in columns]
    if not fields:
        return JoinOrdering(None, None, "ambiguous", "no_columns")
    client = client or JevClient()
    choices = {f"c{index}": {"table": field.table, "column": field.column}
               for index, field in enumerate(fields)}
    target_choices = {
        **choices,
        "none": "No inspected joined column grounds the requested ranking concept.",
    }
    state = {"request": request, "joined_tables": tuple(table_columns)}
    questions = {
        "target": {"type": "choice",
                   "instructions": ("Which qualified inspected column actually grounds the requested "
                                    "ordering? Choose none for an unrepresented ranking concept."),
                   "criteria": target_choices},
        "direction": {"type": "choice", "instructions": "Which order direction is requested?",
                      "criteria": {"ASC": "low to high, early to late, A to Z",
                                   "DESC": "high to low, late to early, Z to A"}},
    }
    call = client.call(state, questions)
    selected = call.answers["target"].get("choice")
    direction = call.answers["direction"].get("choice")
    if selected not in target_choices or direction not in ("ASC", "DESC"):
        raise RuntimeError("Jev selected ordering outside inspected joined columns")
    if selected == "none":
        trace = ({"purpose": "resolve joined ordering", "state": state,
                  "choices": target_choices, "selected": None,
                  "probabilities": {key: call.answers[key].get("probabilities")
                                    for key in questions},
                  "confidence": {key: call.answers[key].get("confidence")
                                  for key in questions},
                  "input_tokens": call.input_tokens,
                  "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        return JoinOrdering(None, None, "ambiguous", "jev_no_grounded_order", 1,
                            call.input_tokens, call.output_tokens, trace)
    field = fields[int(selected[1:])]
    trace = ({"purpose": "resolve joined ordering", "state": state,
              "choices": target_choices, "selected": f"{field.table}.{field.column} {direction}",
              "probabilities": {key: call.answers[key].get("probabilities") for key in questions},
              "confidence": {key: call.answers[key].get("confidence") for key in questions},
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return JoinOrdering(field, direction, "resolved", "jev_choice", 1,
                        call.input_tokens, call.output_tokens, trace)
