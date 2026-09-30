"""Resolve whether an ordered result asks for all rows or one global extremum."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class ResultCardinality:
    mode: str  # all | top_one
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_result_cardinality(
    request: str,
    ordering: dict[str, Any] | None,
    client: JevClient | None = None,
    *, tie_preserving_extremum: bool = False,
) -> ResultCardinality:
    """Distinguish all rows from one global extremum, before or after ordering.

    This decision is deliberately bounded to ALL vs TOP_ONE. Explicit numeric
    limits are resolved elsewhere from exact literal facts. When ``ordering`` is
    None, a TOP_ONE answer means that a singular global extremum is itself strong
    evidence that an ORDER BY must be resolved. Partitioned top-N belongs to the
    unsupported window/ranking branch and never reaches here.
    """
    client = client or JevClient()
    criteria = {
        "all": (
            "Return the complete ordered result set. The wording specifies order, "
            "not a single global winner/extremum."
        ),
        "top_one": (
            "Return only the one globally best/worst/highest/lowest/first/last result "
            "under the resolved ordering."
        ),
    }
    if tie_preserving_extremum:
        criteria["all"] = "Return all source rows tied at the established extremum; the request allows ties"
    state = {
        "request": request,
        "resolved_ordering": ordering,
        "tie_preserving_extremum": tie_preserving_extremum,
        "task": (
            "Decide only global output cardinality. Do not infer top-one merely because "
            "results are sorted. If resolved_ordering is null, choose top_one only when "
            "the request itself clearly asks for one globally best/worst/highest/lowest/"
            "earliest/latest row or entity; that decision will trigger a separate bounded "
            "ordering skill. Otherwise choose all."
        ),
    }
    if tie_preserving_extremum:
        state["task"] = "The extremum already selects winning rows. Decide only whether all tied winners are allowed or exactly one row is requested. This is not a choice between sorting all source rows and finding an extremum."
    call = client.call(state, {"cardinality": {
        "type": "choice",
        "instructions": "Does the request want all ordered rows or only the single global extremum?",
        "criteria": criteria,
    }})
    answer = call.answers["cardinality"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("Jev selected an unknown result cardinality")
    confidence = float(answer.get("confidence", 0.0) or 0.0)
    probabilities = {key: float(value) for key, value in
                     (answer.get("probabilities") or {}).items()}
    top_probability = probabilities.get("top_one", 0)
    all_probability = probabilities.get("all", 0)
    # Truncating to one row changes meaning more severely than returning the
    # ordered set, so require solid confidence or a decisive bounded choice.
    decisive_top_one = (top_probability >= .8 and
                        top_probability - all_probability >= .5)
    accepted = (selected if selected == "all" or confidence >= .72 or
                (selected == "top_one" and decisive_top_one) else "all")
    ambiguous = tie_preserving_extremum and accepted != selected
    trace = ({
        "purpose": "resolve ordered result cardinality",
        "state": state,
        "choices": criteria,
        "selected": selected,
        "accepted": accepted,
        "confidence": confidence,
        "probabilities": answer.get("probabilities"),
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    return ResultCardinality(accepted, "ambiguous" if ambiguous else "resolved",
                             "jev_global_cardinality" if accepted == selected
                             else "conservative_all_on_low_confidence",
                             1, call.input_tokens, call.output_tokens, trace)
