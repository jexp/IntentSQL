"""One bounded repair for a missing NULL predicate after coverage validation."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class FilterRepair:
    column: str | None
    operator: str | None
    status: str
    source: str = "jev_registered_capability"
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _mentioned(request: str, column: str) -> bool:
    words = re.findall(r"[a-z0-9]+", request.casefold())
    parts = re.findall(r"[a-z0-9]+", column.casefold())
    return bool(parts) and any(words[index:index + len(parts)] == parts
                               for index in range(len(words) - len(parts) + 1))


def resolve_missing_null_filter(
    request: str,
    columns: tuple[Column, ...],
    represented: tuple[dict[str, Any], ...],
    client: JevClient,
    *,
    allow_semantic_column_fallback: bool = False,
) -> FilterRepair:
    """Select one legal NULL check, or none; never invent a predicate value."""
    represented_names = {str(item.get("column")) for item in represented
                         if item.get("column")}
    existing = {(str(item.get("column")), str(item.get("operator")))
                for item in represented}
    candidates = tuple(
        column.name for column in columns
        if column.nullable and (column.name in represented_names or
                                _mentioned(request, column.name)))
    fallback = False
    if not candidates and allow_semantic_column_fallback:
        # This path is reached only after final coverage has already concluded
        # that a source-row filter is missing.  Expose inspected nullable columns
        # as bounded recovery choices rather than requiring the request to repeat
        # the physical schema spelling (for example natural "batting side" for
        # a stored ``bats`` column).  Primary keys are excluded because a missing
        # PK is not a meaningful nullable-row repair.
        candidates = tuple(column.name for column in columns
                           if column.nullable and not column.primary_key)
        fallback = bool(candidates)
    criteria: dict[str, Any] = {
        "none": "No missing NULL or non-NULL source-row condition is requested."
    }
    lookup: dict[str, tuple[str, str]] = {}
    for index, column in enumerate(candidates):
        for suffix, operator in (("null", "IS NULL"), ("present", "IS NOT NULL")):
            if (column, operator) in existing:
                continue
            key = f"c{index}_{suffix}"
            criteria[key] = {"column": column, "operator": operator}
            lookup[key] = (column, operator)
    if not lookup:
        return FilterRepair(None, None, "unresolved", "no_grounded_null_candidate")
    state = {
        "request": request,
        "established_predicates": represented,
        "candidate_nullable_columns": candidates,
        "semantic_column_fallback": fallback,
        "task": (
            "Coverage found one missing source-row filter. Select only an explicit "
            "missing-value/present-value condition. Column choices are inspected schema "
            "columns; natural language may describe a column semantically rather than "
            "repeat its physical name. Choose none unless both the column meaning and "
            "NULL/presence polarity are clearly requested."
        ),
    }
    call = client.call(state, {"repair": {
        "type": "choice",
        "instructions": "Which inspected NULL predicate is missing, or none?",
        "criteria": criteria,
    }})
    answer = call.answers["repair"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("System One selected a filter repair outside registered choices")
    confidence = float(answer.get("confidence") or 0)
    threshold = .72 if fallback else .65
    chosen = lookup.get(selected) if confidence >= threshold else None
    trace = ({
        "purpose": "repair one missing NULL predicate",
        "state": state,
        "choices": criteria,
        "selected": selected,
        "confidence": confidence,
        "probabilities": answer.get("probabilities"),
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    return FilterRepair(
        chosen[0] if chosen else None,
        chosen[1] if chosen else None,
        "resolved" if chosen else "unresolved",
        jev_calls=1, input_tokens=call.input_tokens,
        output_tokens=call.output_tokens, trace=trace)
