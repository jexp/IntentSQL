"""Conservatively recover one omitted low-cardinality categorical predicate."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient


_WORDS = re.compile(r"[^\W\d_][\w']*", re.UNICODE)


@dataclass(frozen=True)
class PredicateCompletion:
    column: str | None
    value: str | None
    operator: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _tokens(text: str) -> set[str]:
    return {match.group().casefold() for match in _WORDS.finditer(text)}


def category_modifier_candidates(
    request: str,
    category_hints: dict[str, tuple[str, ...]],
    represented_columns: tuple[str, ...] = (),
) -> tuple[tuple[str, str], ...]:
    """Retrieve possible implicit categories without assigning semantic meaning.

    Only discriminative words from complete, tiny observed category domains are
    used. Words shared by every value in a column are ignored, so a request for
    merely "schools" does not make either "Public School" or "Charter School"
    a candidate; "public schools" does make the former a candidate.
    """
    request_words = _tokens(request)
    represented = set(represented_columns)
    candidates: list[tuple[str, str]] = []
    for column, values in category_hints.items():
        if column in represented or len(values) < 2:
            continue
        token_sets = [_tokens(value) for value in values]
        if not token_sets:
            continue
        shared = set.intersection(*token_sets) if token_sets else set()
        column_words = _tokens(column.replace("_", " "))
        for value, value_words in zip(values, token_sets):
            discriminative = value_words - shared - column_words
            if discriminative and request_words.intersection(discriminative):
                candidates.append((column, value))
    return tuple(candidates)


def resolve_predicate_completion(
    request: str,
    table: str,
    category_hints: dict[str, tuple[str, ...]],
    represented: tuple[dict[str, Any], ...] = (),
    client: JevClient | None = None,
) -> PredicateCompletion:
    """Ask Jev whether one bounded categorical restriction is still missing.

    Candidate generation is deterministic and conservative. Jev can only choose
    an observed column/value pair with INCLUDE (=) or EXCLUDE (!=), or NONE.
    It cannot invent a column, value, or comparison. This lets natural negative
    modifiers such as "not X" or "leave out X" work without phrase rules.
    """
    represented_columns = tuple(str(item.get("column")) for item in represented
                                if item.get("column"))
    candidates = category_modifier_candidates(request, category_hints,
                                               represented_columns)
    if not candidates:
        return PredicateCompletion(None, None, None, "resolved", "no_candidate")

    criteria: dict[str, Any] = {}
    lookup: dict[str, tuple[str, str, str]] = {}
    for index, (column, value) in enumerate(candidates):
        for suffix, operator, meaning in (
            ("include", "=", "include only rows with this observed category"),
            ("exclude", "!=", "exclude rows with this observed category"),
        ):
            key = f"c{index}_{suffix}"
            criteria[key] = {"column": column, "observed_value": value,
                             "operator": operator, "meaning": meaning}
            lookup[key] = (column, value, operator)
    criteria["none"] = "No additional categorical source-row restriction is expressed"
    state = {
        "request": request,
        "source_table": table,
        "already_represented_predicates": represented,
        "candidate_observed_categories": {
            key: value for key, value in criteria.items() if key != "none"
        },
        "task": (
            "Check only for one row restriction expressed by an entity modifier or "
            "category qualifier that is still missing from the represented predicates. "
            "Every candidate value was observed in the selected database. Distinguish "
            "including that category from excluding that category. Do not choose the "
            "closest category; choose NONE unless the request clearly expresses it."
        ),
    }
    client = client or JevClient()
    call = client.call(state, {"missing_category": {
        "type": "choice",
        "instructions": "Which grounded category restriction is still missing, or none?",
        "criteria": criteria,
    }})
    answer = call.answers["missing_category"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("Jev selected an unknown predicate-completeness candidate")
    confidence = float(answer.get("confidence", 0.0) or 0.0)
    chosen: tuple[str, str, str] | None = None
    if selected != "none" and confidence >= .75:
        chosen = lookup[selected]
    trace = ({
        "purpose": "recover one omitted categorical source predicate with polarity",
        "state": state,
        "choices": criteria,
        "selected": selected,
        "confidence": confidence,
        "probabilities": answer.get("probabilities"),
        "accepted": chosen,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    if chosen is None:
        return PredicateCompletion(None, None, None, "resolved",
                                   "jev_none_or_low_confidence", 1,
                                   call.input_tokens, call.output_tokens, trace)
    return PredicateCompletion(chosen[0], chosen[1], chosen[2], "resolved",
                               "jev_schema_grounded_category", 1,
                               call.input_tokens, call.output_tokens, trace)
