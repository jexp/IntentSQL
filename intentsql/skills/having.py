"""Resolve one predicate on a grouped aggregate measure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import extract_facts
from intentsql.skills.quantity import decode_bounded_integer


_OPERATORS = {
    "=": "equal to the threshold",
    "!=": "not equal to the threshold",
    ">": "strictly greater than the threshold",
    ">=": "greater than or equal to the threshold",
    "<": "strictly less than the threshold",
    "<=": "less than or equal to the threshold",
}


@dataclass(frozen=True)
class Having:
    operator: str | None
    value: int | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    fact_id: str | None = None


def resolve_having(request: str, group_key: str, measure: str,
                   client: JevClient | None = None) -> Having:
    client = client or JevClient()
    state = {"request": request, "group_key": group_key,
             "computed_measure": measure,
             "task": "Choose only the comparison that qualifies computed groups after aggregation, not a source-row WHERE condition or result LIMIT."}
    call = client.call(state, {"operator": {"type": "choice",
        "instructions": "How is the computed group measure compared to its threshold?",
        "criteria": _OPERATORS}})
    answer = call.answers["operator"]
    op = answer.get("choice")
    if op not in _OPERATORS:
        raise RuntimeError("Jev selected invalid HAVING operator")
    trace = ({"purpose": "resolve grouped threshold operator", "state": state,
              "choices": _OPERATORS, "selected": op,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    numbers = [fact for fact in extract_facts(request) if fact.kind == "integer"]
    if len(numbers) == 1:
        return Having(op, numbers[0].value, "resolved", "one_numeric_fact", 1,
                      call.input_tokens, call.output_tokens, trace,
                      fact_id=numbers[0].fact_id)
    if len(numbers) > 1:
        choices = {f"n{index}": {"value": fact.value, "offset": fact.start}
                   for index, fact in enumerate(numbers)}
        value_state = {"request": request, "computed_measure": measure}
        follow = client.call(value_state, {"threshold": {"type": "choice",
            "instructions": "Which extracted number is the computed-group threshold?",
            "criteria": choices}})
        selected = follow.answers["threshold"].get("choice")
        if selected not in choices:
            raise RuntimeError("Jev selected invalid HAVING threshold")
        trace += ({"purpose": "assign grouped threshold literal", "state": value_state,
                   "choices": choices, "selected": selected,
                   "probabilities": follow.answers["threshold"].get("probabilities"),
                   "input_tokens": follow.input_tokens,
                   "output_tokens": follow.output_tokens,
                   "elapsed_ms": follow.elapsed_ms},)
        chosen_fact = numbers[int(selected[1:])]
        return Having(op, chosen_fact.value, "resolved", "jev_literal_role", 2,
                      call.input_tokens + follow.input_tokens,
                      call.output_tokens + follow.output_tokens, trace,
                      fact_id=chosen_fact.fact_id)
    decoded = decode_bounded_integer(request,
                                     "exact number stated as the group-measure comparison boundary, not the largest value satisfying it",
                                     client)
    return Having(op, decoded.value, "resolved" if decoded.status == "bounded" else decoded.status,
                  "jev_digit_codec", 1 + decoded.jev_calls,
                  call.input_tokens + decoded.input_tokens,
                  call.output_tokens + decoded.output_tokens,
                  trace + decoded.trace)
