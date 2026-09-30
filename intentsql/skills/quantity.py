"""Resolve a routed row quantity from literal facts or bounded Jev digits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import LiteralFact, extract_facts


@dataclass(frozen=True)
class Quantity:
    value: int | None
    status: str  # bounded, unbounded, unspecified, ambiguous, invalid
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    fact_id: str | None = None


def resolve_quantity(request: str, mode: str, client: JevClient | None = None,
                     facts: tuple[LiteralFact, ...] | None = None,
                     other_numeric_roles: bool = False) -> Quantity:
    """`mode` comes from Jev's intent route; this skill never infers it from words."""
    if mode == "none":
        return Quantity(None, "unspecified", "intent_route")
    if mode == "all":
        return Quantity(None, "unbounded", "intent_route")
    if mode not in ("numeric", "worded"):
        raise ValueError("Unknown quantity mode")
    if mode == "numeric":
        numbers = [fact for fact in (facts if facts is not None else extract_facts(request))
                   if fact.kind == "integer"]
        if not numbers:
            # Jev determines that the request has a row cap; literal syntax
            # determines whether the cap was written with digits or words.
            # A route-label mismatch must not erase an exact extracted fact.
            if any(fact.kind == "worded_number" for fact in
                   (facts if facts is not None else extract_facts(request))):
                return resolve_quantity(request, "worded", client, facts,
                                        other_numeric_roles)
            return Quantity(None, "ambiguous", "no_numeric_literal")
        if len(numbers) == 1 and not other_numeric_roles:
            value = numbers[0].value
            if value < 0 or value > 100_000:
                return Quantity(None, "invalid", "limit_out_of_range")
            return Quantity(value, "bounded", "single_numeric_literal", fact_id=numbers[0].fact_id)
        client = client or JevClient()
        choices = {f"n{index}": {"value": fact.value, "offset": fact.start}
                   for index, fact in enumerate(numbers)}
        choices["worded"] = {"value": "a quantity expressed in words rather than the listed digits"}
        questions = {"limit": {"type": "choice",
                               "instructions": "Which extracted integer is the output row limit?",
                               "criteria": choices}}
        state = {"request": request, "integer_literals": choices}
        call = client.call(state, questions)
        answer = call.answers["limit"]
        selected = answer.get("choice")
        if selected not in choices:
            raise RuntimeError("Jev selected an unknown numeric fact")
        trace = ({"purpose": "assign output quantity to an extracted literal",
                  "state": state, "choices": choices, "selected": selected,
                  "probabilities": answer.get("probabilities"),
                  "confidence": answer.get("confidence"),
                  "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        if selected == "worded":
            decoded = decode_bounded_integer(request, "requested result row count", client)
            return Quantity(decoded.value, decoded.status, "jev_worded_role", 1 + decoded.jev_calls,
                            call.input_tokens + decoded.input_tokens,
                            call.output_tokens + decoded.output_tokens,
                            trace + decoded.trace)
        value = choices[selected]["value"]
        if value < 0 or value > 100_000:
            return Quantity(None, "invalid", "limit_out_of_range", 1,
                            call.input_tokens, call.output_tokens, trace)
        return Quantity(value, "bounded", "jev_literal_role", 1,
                        call.input_tokens, call.output_tokens, trace,
                        numbers[int(selected[1:])].fact_id)

    # Worded cardinalities are already parsed into exact literal facts. Do not
    # ask Jev to regenerate "five" as 005: Jev should assign semantic roles,
    # while deterministic code preserves exact values.
    worded = [fact for fact in (facts if facts is not None else extract_facts(request))
              if fact.kind == "worded_number" and isinstance(fact.value, int)]
    if worded:
        if len(worded) == 1 and not other_numeric_roles:
            value = int(worded[0].value)
            if value < 0 or value > 100_000:
                return Quantity(None, "invalid", "limit_out_of_range")
            return Quantity(value, "bounded", "single_worded_literal", fact_id=worded[0].fact_id)
        client = client or JevClient()
        choices = {f"w{index}": {"value": int(fact.value), "surface": fact.raw,
                                   "offset": fact.start}
                   for index, fact in enumerate(worded)}
        choices["none"] = {"value": None,
                           "meaning": "none of these worded numbers is the global output row count"}
        call = client.call(
            {"request": request, "worded_numeric_literals": choices},
            {"limit": {"type": "choice",
                       "instructions": "Which worded numeric literal is the output row limit?",
                       "criteria": choices}},
        )
        answer = call.answers["limit"]
        selected = answer.get("choice")
        if selected not in choices:
            raise RuntimeError("Jev selected an unknown worded quantity fact")
        trace = ({"purpose": "assign output quantity to an exact worded literal",
                  "state": {"request": request, "worded_numeric_literals": choices},
                  "choices": choices, "selected": selected,
                  "probabilities": answer.get("probabilities"),
                  "confidence": answer.get("confidence"),
                  "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        if selected == "none":
            return Quantity(None, "ambiguous", "jev_no_worded_limit", 1,
                            call.input_tokens, call.output_tokens, trace)
        value = int(choices[selected]["value"])
        if value < 0 or value > 100_000:
            return Quantity(None, "invalid", "limit_out_of_range", 1,
                            call.input_tokens, call.output_tokens, trace)
        return Quantity(value, "bounded", "jev_worded_literal_role", 1,
                        call.input_tokens, call.output_tokens, trace,
                        worded[int(selected[1:])].fact_id)

    # Keep the bounded digit codec as a fallback for semantic quantities that
    # the conservative deterministic parser does not recognize.
    return decode_bounded_integer(request, "requested result row count", client)


def decode_bounded_integer(request: str, role: str,
                           client: JevClient | None = None) -> Quantity:
    """Compose one semantic 0–1000 integer from three independent choices."""
    client = client or JevClient()
    digits = {str(value): None for value in range(10)}
    questions = {
        "h": {"type": "choice", "instructions": f"hundreds digit of {role}",
              "criteria": {**digits, "10": None}},
        "t": {"type": "choice", "instructions": f"tens digit of {role}",
              "criteria": digits},
        "o": {"type": "choice", "instructions": f"ones digit of {role}",
              "criteria": digits},
    }
    state = {"request": request, "semantic_number_role": role}
    call = client.call(state, questions)
    answers = call.answers
    selected = {key: answers[key].get("choice") for key in ("h", "t", "o")}
    for key in selected:
        if selected[key] not in questions[key]["criteria"]:
            raise RuntimeError("Jev selected an unknown quantity digit")
    value = 100 * int(selected["h"]) + 10 * int(selected["t"]) + int(selected["o"])
    trace = ({"purpose": "compose worded quantity from bounded digits",
              "state": state, "selected": selected,
              "probabilities": {key: answers[key].get("probabilities") for key in selected},
              "confidence": {key: answers[key].get("confidence") for key in selected},
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    if value > 1000:
        return Quantity(None, "invalid", "codec_out_of_range", 1,
                        call.input_tokens, call.output_tokens, trace)
    return Quantity(value, "bounded", "jev_digit_codec", 1,
                    call.input_tokens, call.output_tokens, trace)


def decode_scaled_number(request: str, role: str,
                         client: JevClient | None = None) -> Quantity:
    """Compose a worded amount from a bounded mantissa and unit scale."""
    client = client or JevClient()
    digits = {str(value): None for value in range(10)}
    questions = {
        "h": {"type": "choice", "instructions": f"hundreds digit of the amount for {role}, before scale",
              "criteria": digits},
        "t": {"type": "choice", "instructions": f"tens digit of the amount for {role}, before scale",
              "criteria": digits},
        "o": {"type": "choice", "instructions": f"ones digit of the amount for {role}, before scale",
              "criteria": digits},
        "scale": {"type": "choice", "instructions": f"unit multiplier for {role}",
                  "criteria": {"1": "units", "1000": "thousands",
                               "1000000": "millions", "1000000000": "billions"}},
    }
    state = {"request": request, "semantic_number_role": role}
    call = client.call(state, questions)
    selected = {key: call.answers[key].get("choice") for key in questions}
    if any(selected[key] not in questions[key]["criteria"] for key in questions):
        raise RuntimeError("Jev selected an unknown numeric component")
    value = (100 * int(selected["h"]) + 10 * int(selected["t"]) +
             int(selected["o"])) * int(selected["scale"])
    return Quantity(value, "bounded", "jev_scaled_codec", 1,
                    call.input_tokens, call.output_tokens,
                    ({"purpose": "compose worded scaled amount", "state": state,
                      "selected": selected,
                      "probabilities": {key: call.answers[key].get("probabilities")
                                        for key in questions},
                      "input_tokens": call.input_tokens,
                      "output_tokens": call.output_tokens,
                      "elapsed_ms": call.elapsed_ms},))
