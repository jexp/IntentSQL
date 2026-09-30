"""Check grouped clauses once the aggregate measure is known."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import extract_facts


@dataclass(frozen=True)
class GroupClauses:
    having: bool
    ordering: bool
    jev_calls: int
    input_tokens: int
    output_tokens: int
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def threshold_candidates(request: str) -> tuple[dict[str, Any], ...]:
    """Keep exact numbers that are not already paired as interval bounds."""
    facts = [fact for fact in extract_facts(request)
             if fact.kind in {"integer", "number", "worded_number"}]
    bounds: set[str] = set()
    for lower, upper in zip(facts, facts[1:]):
        before = request[max(0, lower.start - 24):lower.start]
        between = request[lower.end:upper.start]
        if (re.search(r"\b(?:between|from)\s*$", before, re.I) and
                re.fullmatch(r"\s*(?:and|to|through|-)\s*", between, re.I)):
            bounds.update((lower.fact_id, upper.fact_id))
    return tuple({"id": fact.fact_id, "value": fact.value, "span": [fact.start, fact.end]}
                 for fact in facts if fact.fact_id not in bounds)


def resolve_group_clauses(request: str, group_key: str, measure: str,
                          client: JevClient, *, excluded_fact_ids: tuple[str, ...] = ()) -> GroupClauses:
    candidates = tuple(item for item in threshold_candidates(request)
                       if item["id"] not in excluded_fact_ids)
    state = {"request": request, "group_key": group_key,
             "computed_measure": measure,
             "candidate_group_thresholds": candidates,
             "task": "A HAVING threshold compares the aggregate value with a cutoff. A requested number of returned groups is LIMIT, not HAVING; ranking groups is ORDER BY."}
    questions = {
        "having": {"type": "noul", "instructions": "Does a threshold on the computed measure restrict the groups?"},
        "ordering": {"type": "noul", "instructions": "Does the request order or rank the resulting groups?"},
        "ordering_basis": {"type": "choice",
            "instructions": "What ordering relationship, if any, is explicitly required among the resulting groups?",
            "criteria": {
                "none": "No requested ordering or ranking of groups.",
                "key": "Order/rank groups by the group key itself.",
                "measure": "Order/rank groups by the computed aggregate measure, including a singular global extremum such as the group with the highest/lowest measure.",
            }},
    }
    threshold_choices = {item["id"]: {"value": item["value"], "span": item["span"],
                                      "role": "cutoff on the computed aggregate value"}
                         for item in candidates}
    threshold_choices["none"] = "No numeric cutoff on a group measure; quantities may instead bound the number of output groups or source rows"
    questions["threshold_fact"] = {"type": "choice",
        "instructions": "Which exact literal is a cutoff on a group aggregate value, rather than an output row count, or none?",
        "criteria": threshold_choices}
    call = client.call(state, questions)
    scores = {name: float(call.answers[name]["noul"]) for name in ("having", "ordering")}
    ordering_basis = call.answers["ordering_basis"].get("choice")
    if ordering_basis not in ("none", "key", "measure"):
        raise RuntimeError("Jev selected an invalid grouped ordering basis")
    ordering = scores["ordering"] >= .6 or ordering_basis != "none"
    threshold = call.answers["threshold_fact"].get("choice")
    if threshold not in threshold_choices:
        raise RuntimeError("Jev selected an ungrounded group threshold")
    return GroupClauses(threshold != "none" and scores["having"] >= .6, ordering,
                        1, call.input_tokens, call.output_tokens,
                        ({"purpose": "check grouped result clauses", "state": state,
                          "scores": scores, "ordering_basis": ordering_basis,
                          "threshold_fact": threshold,
                          "probabilities": {
                              "ordering_basis": call.answers["ordering_basis"].get("probabilities")},
                          "input_tokens": call.input_tokens,
                          "output_tokens": call.output_tokens,
                          "elapsed_ms": call.elapsed_ms},))
