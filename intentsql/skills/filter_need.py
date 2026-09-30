"""Refine whether source rows are restricted after schema selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class FilterNeed:
    needed: bool
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_filter_need(request: str, table: str, columns: tuple[Column, ...],
                        client: JevClient | None = None,
                        group_measure_filter: bool = False,
                        category_hints: dict[str, tuple[str, ...]] | None = None,
                        relational_selector: dict[str, Any] | None = None,
                        literal_facts: tuple[dict[str, Any], ...] = (),
                        established_groups: tuple[str, ...] = (),
                        established_measure: str | None = None,
                        candidate_value_evidence: dict[str, tuple[str, ...]] | None = None,
                        duplicate_candidate: dict[str, Any] | None = None) -> FilterNeed:
    client = client or JevClient()
    state = {"request": request, "selected_table": table,
             "available_columns": [column.name for column in columns],
             "group_measure_filter": group_measure_filter}
    if category_hints:
        state["small_category_values"] = category_hints
    if candidate_value_evidence:
        state["request_related_observed_values"] = candidate_value_evidence
    if relational_selector:
        state["already_represented_row_selector"] = relational_selector
    if literal_facts:
        state["exact_request_literals"] = literal_facts
    if established_groups:
        state["established_group_keys"] = established_groups
    if established_measure:
        state["established_computed_measure"] = established_measure
    if relational_selector and relational_selector.get("unresolved_candidate_columns"):
        candidates = set(relational_selector["unresolved_candidate_columns"])
        choices = {column.name: {"column": column.name, "type": column.declared_type}
                   for column in columns if column.name in candidates}
        choices["none"] = "No additional WHERE condition; the remaining wording is output, ordering or result count"
        call = client.call(state, {"missing_condition": {"type": "choice",
            "instructions": "Which candidate column has a WHERE restriction not already represented, or none? Ignore sorting and LIMIT.",
            "criteria": choices}})
        selected = call.answers["missing_condition"].get("choice")
        if selected not in choices:
            raise RuntimeError("Jev selected an uninspected additional predicate")
        trace = ({"purpose": "review only unresolved predicate candidates", "state": state,
                  "choices": choices, "selected": selected,
                  "probabilities": call.answers["missing_condition"].get("probabilities"),
                  "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        return FilterNeed(selected != "none", "resolved", "jev_predicate_tail_choice", 1,
                          call.input_tokens, call.output_tokens, trace)
    if duplicate_candidate:
        state["candidate_additional_condition"] = duplicate_candidate
        call = client.call(state, {"additional": {"type": "noul",
            "instructions": "Is this candidate a separate requested row condition beyond the already bound condition, despite using the same operand?"}})
        score = float(call.answers["additional"]["noul"])
        trace = ({"purpose": "check duplicate-operand condition role", "state": state,
                  "score": score, "input_tokens": call.input_tokens,
                  "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        return FilterNeed(score >= .6, "ambiguous" if .3 < score < .6 else "resolved",
                          "jev_duplicate_condition_review", 1,
                          call.input_tokens, call.output_tokens, trace)
    if ((group_measure_filter and (category_hints or len(columns) > 1)) or
            category_hints or candidate_value_evidence):
        choices = {f"c{index}": {
            "column": column.name,
            "request_related_values": (candidate_value_evidence or {}).get(column.name, ()),
        } for index, column in enumerate(columns)}
        choices["none"] = ("No source-row restriction; the threshold applies after grouping"
                           if group_measure_filter else
                           "No additional stored-row restriction")
        state["task"] = ("Choose the source column, if any, with an explicit row restriction. "
                          "Observed values are candidates, not proof of a filter. Ignore "
                          "output, ordering, and limits."
                          if not group_measure_filter else
                          "Choose the source column with a row restriction independent of the grouped measure. Exact literals are unassigned evidence; a group threshold is not WHERE.")
        call = client.call(state, {"filter_column": {"type": "choice",
            "instructions": ("Which column has a source-row restriction before grouping, or none?"
                             if group_measure_filter else
                             "Which column has an additional source-row restriction, or none?"),
            "criteria": choices}})
        selected = call.answers["filter_column"].get("choice")
        needed = selected in choices and selected != "none"
        trace = ({"purpose": "resolve grouped source-filter presence", "state": state,
                  "choices": choices, "selected": selected,
                  "input_tokens": call.input_tokens,
                  "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        return FilterNeed(needed, "resolved", "jev_group_filter_choice", 1,
                          call.input_tokens, call.output_tokens, trace)
    call = client.call(state, {"filter": {"type": "noul",
        "instructions": ("Is there an independent stored-row restriction before grouping?"
                         if group_measure_filter else
                         "Is there an unrepresented stored-value row restriction?")}})
    score = float(call.answers["filter"]["noul"])
    trace = ({"purpose": "refine source filter need from selected schema",
              "state": state, "score": score,
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return FilterNeed(score >= .6, "resolved", "jev_noul", 1,
                      call.input_tokens, call.output_tokens, trace)
