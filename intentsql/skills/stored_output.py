"""Distinguish a stored numeric output from a computed row tally."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class StoredOutput:
    stored: bool
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_stored_output(request: str, table_columns: dict[str, tuple[Column, ...]],
                          client: JevClient | None = None) -> StoredOutput:
    """Use selected and directly reachable columns, never a full database dump."""
    if not table_columns:
        return StoredOutput(False, "ambiguous", "no_columns")
    client = client or JevClient()
    state = {"request": request,
             "inspected_columns": {table: [column.name for column in columns]
                                   for table, columns in table_columns.items()}}
    call = client.call(state, {"stored": {
        "type": "noul",
        "instructions": "Is the requested numeric output stored, rather than computed from rows?"}})
    score = float(call.answers["stored"]["noul"])
    trace = ({"purpose": "distinguish stored numeric output from computed row count",
              "state": state, "score": score, "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens, "elapsed_ms": call.elapsed_ms},)
    if score >= .6:
        return StoredOutput(True, "resolved", "jev_noul", 1,
                            call.input_tokens, call.output_tokens, trace)
    if score < .35:
        return StoredOutput(False, "resolved", "jev_noul", 1,
                            call.input_tokens, call.output_tokens, trace)

    # Borderline Noul scores are common when words such as "count" can either
    # mean SQL COUNT(*) or name an already-stored measure.  Do not guess from
    # the word alone. Give Jev one focused bounded choice over inspected
    # numeric attributes versus a computed row tally.
    numeric_fields = tuple(
        f"{table}.{column.name}"
        for table, columns in table_columns.items()
        for column in columns
        if any(kind in column.declared_type.upper()
               for kind in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))
    )
    if not numeric_fields:
        return StoredOutput(False, "ambiguous", "jev_noul_borderline", 1,
                            call.input_tokens, call.output_tokens, trace)
    review_state = {
        "request": request,
        "inspected_numeric_fields": numeric_fields,
        "task": ("Decide whether the requested numeric value is an existing stored "
                 "attribute from these inspected fields or a row count that must be "
                 "computed. Do not invent a field or value."),
    }
    choices = {
        "stored": "An inspected numeric field stores the requested value for each entity/row.",
        "computed_count": "The request asks to count matching rows/records with SQL COUNT(*).",
        "ambiguous": "The request does not safely distinguish the two meanings.",
    }
    review = client.call(review_state, {"kind": {
        "type": "choice",
        "instructions": "Is this stored numeric output or a computed row count?",
        "criteria": choices,
    }})
    selected = review.answers["kind"].get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected an invalid stored-output refinement")
    trace += ({"purpose": "refine borderline stored-vs-count semantics",
               "state": review_state, "choices": choices, "selected": selected,
               "probabilities": review.answers["kind"].get("probabilities"),
               "confidence": review.answers["kind"].get("confidence"),
               "input_tokens": review.input_tokens,
               "output_tokens": review.output_tokens,
               "elapsed_ms": review.elapsed_ms},)
    return StoredOutput(selected == "stored",
                        "resolved" if selected != "ambiguous" else "ambiguous",
                        "jev_stored_output_refinement", 2,
                        call.input_tokens + review.input_tokens,
                        call.output_tokens + review.output_tokens, trace)
