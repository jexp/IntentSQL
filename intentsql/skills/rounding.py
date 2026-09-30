"""Resolve a bounded rounding request without generating SQL or expressions."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import extract_facts


@dataclass(frozen=True)
class Rounding:
    places: int | None
    status: str  # resolved, unsupported
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    fact_id: str | None = None


def _rounding_fact_id(request: str, places: int) -> str | None:
    """Return the exact literal owned by rounding when provenance is unambiguous.

    Rounding is a transformation parameter, not a source-row predicate.  Once
    the bounded rounding skill has selected the number of places, preserve the
    literal's provenance so later predicate binding cannot reuse the same
    operand as a WHERE value.  Ambiguous repeated literals remain unclaimed.
    """
    matches = [fact for fact in extract_facts(request)
               if fact.kind in {"integer", "number", "worded_number"}
               and fact.value == places]
    if len(matches) != 1:
        return None
    fact = matches[0]
    context = request[max(0, fact.start - 32):min(len(request), fact.end + 32)]
    if not re.search(r"\b(?:round(?:ed|ing)?|decimal|place|digit)\w*\b",
                     context, re.IGNORECASE):
        return None
    return fact.fact_id


def resolve_rounding(request: str, measure: str,
                     client: JevClient | None = None) -> Rounding:
    """Select decimal places only if rounding the given measure is the whole transformation."""
    client = client or JevClient()
    choices = {str(places): None for places in range(7)}
    choices["unsupported"] = "not solely rounding this measure"
    state = {"request": request, "measure": measure}
    questions = {"rounding": {"type": "choice",
                              "instructions": "How many decimal places, or is this another transformation?",
                              "criteria": choices}}
    call = client.call(state, questions)
    answer = call.answers["rounding"]
    selected = answer.get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected an unknown rounding choice")
    trace = ({"purpose": "resolve a bounded aggregate rounding transformation",
              "state": state, "choices": choices, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    places = None if selected == "unsupported" else int(selected)
    return Rounding(places,
                    "unsupported" if selected == "unsupported" else "resolved",
                    "jev_choice", 1, call.input_tokens, call.output_tokens, trace,
                    _rounding_fact_id(request, places) if places is not None else None)
