"""Resolve output kind before independent remaining branch indicators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from intentsql.jev_client import JevClient


_CHOICES = {
    "output": {
        "rows": "Complete source records with all fields",
        "fields": "Selected stored fields from source records",
        "count": "COUNT of matching records, overall or per group",
        "other": "SUM, AVG, MIN, or MAX of stored values",
    },
    "quantity": {
        "none": "No requested maximum number of output records",
        "numeric": "Maximum output count is written with numeral digits",
        "worded": "Maximum output count is expressed without numeral digits",
        "all": "Explicitly return all matching records",
    },
}

_YES_NO = {
    "filters": "Does the request restrict WHICH SOURCE RECORDS qualify (a WHERE condition)?",
    "ordering": "Does the request specify HOW output records should be ordered?",
    "distinct": "Does the request require deduplicating equal output values?",
    "grouping": "Is a measure computed separately for each group?",
    "relationship": "Does the request require combining facts from more than one relation/table?",
    "having": "Does a computed measure restrict which groups qualify?",
    "expression": "Does the output transform a value beyond a plain aggregate?",
    "windowing": "Does the request need row-relative or per-partition ranking?",
}

_MEANING = {
    "filters": "Stored-value conditions that limit source rows; excludes output, order, and limit.",
    "ordering": "Sequence output rows by a criterion.",
    "distinct": "Remove repeated equal output values.",
    "grouping": "Compute an aggregate separately per category.",
    "relationship": "Use a value stored in another table.",
    "having": "Filter groups by their computed measure, not stored row values.",
    "expression": "Transform an output beyond direct fields or a plain aggregate.",
    "windowing": "Rank or calculate rows relative to other rows in each partition.",
}


@dataclass(frozen=True)
class Intent:
    output: str
    filters: bool
    ordering: bool
    quantity: str
    distinct: bool
    grouping: bool
    relationship: bool
    having: bool
    expression: bool
    windowing: bool
    scores: dict[str, float]
    confidence: dict[str, float]
    probabilities: dict[str, dict[str, float]]
    input_tokens: int
    output_tokens: int
    elapsed_ms: int
    raw_answers: dict[str, Any]
    jev_calls: int = 1

    @property
    def relevant_branches(self) -> tuple[str, ...]:
        branches = ["entity"]
        if self.output == "fields":
            branches.append("projection")
        elif self.output in ("count", "other"):
            branches.append("aggregation")
        if self.filters:
            branches.append("predicate")
        if self.ordering:
            branches.append("ordering")
        if self.quantity in ("numeric", "worded"):
            branches.append("quantity")
        if self.distinct:
            branches.append("distinct")
        if self.grouping:
            branches.append("grouping")
        if self.relationship:
            branches.append("relationship")
        if self.having:
            branches.append("having")
        if self.expression:
            branches.append("expression")
        if self.windowing:
            branches.append("windowing")
        return tuple(branches)


def route_intent(request: str, client: JevClient | None = None, *,
                 source_table: str | None = None, columns: tuple = (), facts: tuple = ()) -> Intent:
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be nonempty text")
    client = client or JevClient()
    questions = {
        key: {"type": "choice", "instructions": f"Classify {key} needed by the request.",
              "criteria": choices}
        for key, choices in _CHOICES.items()
    }
    questions["output"]["instructions"] = "What is returned? Choose fields only for requested output attributes, not attributes mentioned solely for filtering or sorting. Returning entities/records without an explicit field list is rows."
    questions.update({key: {"type": "noul", "instructions": instruction}
                      for key, instruction in _YES_NO.items()})
    state = {"request": request, "meaning": _MEANING}
    if source_table:
        state["established_source"] = source_table
        state["stored_columns"] = [{"name": col.name, "type": col.declared_type} for col in columns]
        state["literal_facts"] = [{"kind": fact.kind, "value": fact.value} for fact in facts]
        state["task"] = ("Classify only the independent query-shape properties asked below. "
                         "Schema is evidence; do not bind columns or values here.")
    # These route questions all inspect the same request/state and none needs a
    # prior answer to construct its options. System One evaluates batched
    # questions independently, so one speculative fan-out call is both faster
    # and cheaper than serializing the output decision first.
    call = client.call(state, questions)
    answers = call.answers
    selected = {}
    confidence = {}
    probabilities = {}
    for key, choices in _CHOICES.items():
        value = answers[key].get("choice")
        if value not in choices:
            raise RuntimeError(f"Jev selected an unknown {key} choice")
        selected[key] = value
        confidence[key] = float(answers[key].get("confidence") or 0)
        probabilities[key] = {item: float(score) for item, score in
                              (answers[key].get("probabilities") or {}).items()}
    scores = {key: float(answers[key]["noul"]) for key in _YES_NO}
    return Intent(
        output=selected["output"], filters=scores["filters"] >= 0.6,
        ordering=scores["ordering"] >= 0.6, quantity=selected["quantity"],
        distinct=scores["distinct"] >= 0.6,
        grouping=scores["grouping"] >= 0.6,
        relationship=scores["relationship"] >= 0.6,
        having=scores["having"] >= 0.6,
        expression=scores["expression"] >= 0.6,
        windowing=scores["windowing"] >= 0.6,
        scores=scores, confidence=confidence, probabilities=probabilities,
        input_tokens=call.input_tokens, output_tokens=call.output_tokens,
        elapsed_ms=call.elapsed_ms, raw_answers=answers,
    )
