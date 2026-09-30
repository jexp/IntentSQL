"""Choose how separately resolved source-row conditions combine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class ConditionLogic:
    connector: str
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_condition_logic(request: str, columns: tuple[str, ...],
                            client: JevClient | None = None,
                            evidence: tuple[dict[str, Any], ...] = ()) -> ConditionLogic:
    if len(columns) < 2:
        return ConditionLogic("AND", "resolved", "single_condition")
    client = client or JevClient()
    choices = {"AND": "Every condition must hold for a row",
               "OR": "At least one condition must hold for a row",
               "complex": "Nested, mixed, or negated Boolean structure is required"}
    state = {"request": request, "condition_columns": columns,
             "bound_condition_evidence": evidence,
             "task": (
                 "Distinguish alternatives across different columns from alternatives of one column. "
                 "Preserve a cross-column OR when the request permits either condition. "
                 "A negative comparison already represented by an atomic operator such as != or "
                 "IS NOT NULL is not, by itself, complex Boolean logic; decide only how the fully "
                 "bound atomic conditions combine."
             )}
    call = client.call(state, {"logic": {"type": "choice",
        "instructions": "How do the independently constrained columns combine to qualify source rows?",
        "criteria": choices}})
    answer = call.answers["logic"]
    selected = answer.get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected invalid condition logic")
    trace = ({"purpose": "combine source-row conditions", "state": state,
              "choices": choices, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens

    # A broad semantic pass can over-read a lexical negation ("not public",
    # "excluding cancelled") as nested Boolean NOT even after the predicate
    # resolver has already encoded that meaning safely as !=. Re-check only
    # when every bound condition is an ordinary atomic predicate. Real nested
    # negation can still remain complex and will continue to fail closed.
    atomic_ops = {"=", "!=", ">", ">=", "<", "<=", "BETWEEN", "IN",
                  "CONTAINS", "PREFIX", "SUFFIX", "IS NULL", "IS NOT NULL"}
    if selected == "complex" and evidence and all(
            item.get("operator") in atomic_ops for item in evidence):
        review_state = {
            "request": request,
            "bound_atomic_conditions": evidence,
            "task": (
                "Each listed condition is already a complete atomic predicate, including any "
                "local negation encoded in != or IS NOT NULL. Decide only whether these atomic "
                "predicates combine with AND, OR, or still require genuinely nested/mixed Boolean "
                "scope such as NOT(A OR B) or (A AND B) OR C."
            ),
        }
        review = client.call(review_state, {"logic": {"type": "choice",
            "instructions": "How do these already-bound atomic predicates combine?",
            "criteria": choices}})
        revised = review.answers["logic"].get("choice")
        if revised not in choices:
            raise RuntimeError("Jev selected invalid condition logic refinement")
        selected = revised
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "distinguish atomic negation from nested Boolean logic",
                   "state": review_state, "choices": choices, "selected": selected,
                   "probabilities": review.answers["logic"].get("probabilities"),
                   "confidence": review.answers["logic"].get("confidence"),
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)

    return ConditionLogic(selected, "unsupported" if selected == "complex" else "resolved",
                          "jev_choice", calls, input_tokens, output_tokens, trace)
