"""Resolve several explicitly named values of one inspected column."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import LiteralFact
from intentsql.skills.range_value import explicit_inclusive_interval
from intentsql.skills.value_hints import mechanically_related_values


@dataclass(frozen=True)
class Alternatives:
    values: tuple[str | int | float, ...]
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    fact_ids: tuple[str, ...] = ()


def resolve_alternatives(request: str, column: str, hints: tuple[str, ...],
                         client: JevClient, *,
                         numeric_facts: tuple[LiteralFact, ...] = (),
                         other_columns: tuple[str, ...] = ()) -> Alternatives:
    # Retrieval is lexical only. Jev decides which observed values actually
    # qualify; two selected values become one typed IN predicate.
    evidence: dict[str, dict] = {}
    if numeric_facts:
        # When two already selected columns are explicitly named, a number
        # following the other column is not an alternative value of this one.
        # Keep all facts when this column is only implied by the request.
        def mentions(name: str) -> list[int]:
            label = re.escape(name).replace("_", r"[_\s]+")
            return [match.start() for match in re.finditer(
                r"(?<!\w)" + label + r"(?!\w)", request, re.I)]
        own = mentions(column)
        if own and other_columns:
            positions = [(position, name) for name in (column, *other_columns)
                         for position in mentions(name)]
            numeric_facts = tuple(fact for fact in numeric_facts
                if not (earlier := [(position, name) for position, name in positions
                                    if position < fact.start]) or
                max(earlier)[1] == column)
        interval = explicit_inclusive_interval(request, numeric_facts)
        interval_ids = {fact.fact_id for fact in interval} if interval else set()
        visible = tuple(dict.fromkeys(
            fact.value for fact in numeric_facts if fact.fact_id not in interval_ids))
    else:
        related = mechanically_related_values(request, hints, column_name=column)
        visible = tuple(dict.fromkeys(value for value, _ in related))
        evidence = {value: detail for value, detail in related}
    if len(visible) < 2:
        return Alternatives(())
    if len(visible) > 16:
        raise ValueError("Too many candidate alternative values; narrow the request.")
    state = {"request": request, "condition_column": column,
             "retrieved_observed_values": tuple(
                 {"value": value, "evidence": evidence.get(str(value),
                  {"kind": "exact_numeric_literal"})} for value in visible)}
    questions = {"relationship": {"type": "choice",
        "instructions": "How do these candidate values constrain this column?",
        "criteria": {
            "alternatives": "Two or more exact equality alternatives; any selected value qualifies independently (a set, not an interval).",
            "other": "An excluded value, range endpoints, inequality, different-column condition, grouping, ordering, output limit, or only one equality value."
        }}}
    questions.update({f"v{index}": {"type": "noul",
                 "instructions": f"Is {value!r} one of the alternative exact values allowed for {column} in the source-row condition?"}
                 for index, value in enumerate(visible)})
    call = client.call(state, questions)
    relationship = call.answers["relationship"].get("choice")
    if relationship not in ("alternatives", "other"):
        raise RuntimeError("Invalid alternative-value relationship")
    selected = tuple(value for index, value in enumerate(visible)
                     if relationship == "alternatives" and float(call.answers[f"v{index}"]["noul"]) >= .6)
    if relationship == "alternatives" and len(selected) < 2:
        raise ValueError("The alternative values are unclear; no partial query was executed.")
    return Alternatives(selected if len(selected) >= 2 else (), 1,
                        call.input_tokens, call.output_tokens,
                        ({"purpose": "resolve same-column alternatives", "state": state,
                          "scores": {str(value): call.answers[f"v{index}"]["noul"]
                                     for index, value in enumerate(visible)},
                          "selected": selected, "input_tokens": call.input_tokens,
                          "output_tokens": call.output_tokens,
                          "elapsed_ms": call.elapsed_ms},),
                        tuple(fact.fact_id for fact in numeric_facts if fact.value in selected))
