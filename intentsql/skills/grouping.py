"""Resolve one GROUP BY key from the selected table schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class GroupKey:
    column: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_extra_group_key(request: str, table: str, primary: str,
                            columns: tuple[Column, ...], client: JevClient) -> GroupKey:
    """Refine a grouped query only when another independent category is possible."""
    candidates = {f"c{index}": col.name for index, col in enumerate(columns)
                  if col.name != primary}
    candidates["none"] = None
    state = {"request": request, "source_table": table, "already_grouped_by": primary}
    call = client.call(state, {"extra": {"type": "choice",
        "instructions": "Which second grouping key is requested, or none?",
        "criteria": {key: (name if name else "No second grouping attribute")
                     for key, name in candidates.items()}}})
    answer = call.answers["extra"]
    selected = answer.get("choice")
    if selected not in candidates:
        raise RuntimeError("Jev selected an unknown grouping attribute")
    return GroupKey(candidates[selected], "resolved", "jev_choice", 1,
                    call.input_tokens, call.output_tokens,
                    ({"purpose": "check second grouping attribute", "state": state,
                      "choices": candidates, "selected": candidates[selected],
                      "probabilities": answer.get("probabilities"),
                      "input_tokens": call.input_tokens,
                      "output_tokens": call.output_tokens,
                      "elapsed_ms": call.elapsed_ms},))


def resolve_group_key(request: str, table: str, columns: tuple[Column, ...],
                      client: JevClient | None = None) -> GroupKey:
    if not columns:
        return GroupKey(None, "ambiguous", "no_columns")
    if len(columns) == 1:
        return GroupKey(columns[0].name, "resolved", "only_column")
    client = client or JevClient()
    choices = {f"c{index}": {"column": column.name,
                              "declared_type": column.declared_type}
               for index, column in enumerate(columns)}
    state = {"request": request, "source_table": table}
    call = client.call(state, {"group": {"type": "choice",
        "instructions": "Which inspected column defines each separate result group or category?",
        "criteria": choices}})
    answer = call.answers["group"]
    selected = answer.get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected a group key outside inspected columns")
    column = choices[selected]["column"]
    trace = ({"purpose": "resolve grouping key", "state": state,
              "choices": choices, "selected": column,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return GroupKey(column, "resolved", "jev_choice", 1,
                    call.input_tokens, call.output_tokens, trace)
