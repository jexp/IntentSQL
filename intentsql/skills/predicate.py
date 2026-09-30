"""Small predicate decisions over one inspected table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import re

from intentsql.jev_client import JevClient
from intentsql.capabilities import comparison_choices
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class PredicateColumn:
    column: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PredicateOperator:
    operator: str
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PredicateStructure:
    count: str
    status: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_predicate_structure(request: str,
                                client: JevClient | None = None,
                                group_measure: str | None = None,
                                available_columns: tuple[str, ...] = ()) -> PredicateStructure:
    """Guard the one-predicate compiler against silently dropping conditions."""
    client = client or JevClient()
    choices = {"one": "One independent row qualification",
               "multiple": "Two or more independent row qualifications"}
    state = {"request": request,
             "condition_meaning": "Count distinct WHERE restrictions on source rows. One inclusive interval counts once. Two alternative values of one column count as one column restriction. Ignore SELECT, ORDER BY, LIMIT and HAVING.",
             "available_columns": available_columns,
             "request_logic_evidence": re.findall(r"\b(?:and|or|not|either)\b", request.casefold())}
    if group_measure:
        state["grouped_measure"] = group_measure
        state["condition_meaning"] += " A threshold on the computed group measure is HAVING, never WHERE."
    call = client.call(state, {"structure": {"type": "choice",
        "instructions": "How many independent source-row restrictions are requested?",
        "criteria": choices}})
    answer = call.answers["structure"]
    selected = answer.get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected invalid predicate structure")
    trace = ({"purpose": "resolve predicate structure", "state": state,
              "choices": choices, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
    if selected == "one" and len(state["request_logic_evidence"]) > 0:
        review_state = {**state,
                        "task": "Reconsider the count with the extracted Boolean syntax and the available columns. Do not collapse independent alternatives on different columns into one restriction."}
        review = client.call(review_state, {"structure": {"type": "choice",
            "instructions": "Are there one or multiple independent row restrictions?",
            "criteria": choices}})
        revised = review.answers["structure"].get("choice")
        if revised in choices:
            selected = revised
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review predicate structure with Boolean evidence",
                   "state": review_state, "choices": choices, "selected": selected,
                   "probabilities": review.answers["structure"].get("probabilities"),
                   "confidence": review.answers["structure"].get("confidence"),
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
    return PredicateStructure(selected, "resolved", calls,
                              input_tokens, output_tokens, trace)


def column_affinity(column: Column) -> str:
    declared = column.declared_type.upper()
    if any(fragment in declared for fragment in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "numeric"
    return "text"


def resolve_predicate_operator(request: str, column: Column,
                               client: JevClient | None = None,
                               value_hints: tuple[str, ...] = (),
                               *, date_semantics: bool = False,
                               pattern_hints: tuple[str, ...] = ()) -> PredicateOperator:
    """Choose a universal relational operator after the column is known."""
    choices = comparison_choices("date" if date_semantics else column_affinity(column))
    choices.update(none="No independent row comparison", ambiguous="No safe comparison")
    client = client or JevClient()
    state = {"request": request, "condition_column": column.name,
             "declared_type": column.declared_type,
             "semantic_type": "iso_date" if date_semantics else column_affinity(column),
             "task": "Determine only this column's comparison. Ignore conditions on other columns and the Boolean connector between them. A single exact column value uses equality; range operators require a stated range or ordering relation."}
    if date_semantics:
        state["task"] += " Treat this inspected column as an ISO calendar date. A year-only condition refers to that calendar year; never use text prefix/contains/suffix matching for it."
    if value_hints:
        state["targeted_value_examples"] = value_hints
    if pattern_hints:
        state["recurring_stored_annotations"] = pattern_hints
    questions = {"operator": {"type": "choice",
                              "instructions": "Which comparison on this column expresses the requested row condition?",
                              "criteria": choices}}
    call = client.call(state, questions)
    answer = call.answers["operator"]
    selected = answer.get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected an invalid predicate operator")
    trace = ({"purpose": "resolve predicate operator", "state": state,
              "choices": choices, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
    probabilities = answer.get("probabilities") or {}
    ranked = sorted(((key, float(probabilities.get(key) or 0)) for key in choices),
                    key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < .18:
        shortlist = {key: choices[key] for key, _ in ranked[:2]}
        shortlist["ambiguous"] = choices["ambiguous"]
        follow_state = {"request": request, "condition_column": column.name,
                        "semantic_type": "iso_date" if date_semantics else column_affinity(column),
                        "task": "Select the comparison applied to this column alone; ignore other columns, output fields and Boolean connectors."}
        if value_hints:
            follow_state["targeted_value_examples"] = value_hints
        if pattern_hints:
            follow_state["recurring_stored_annotations"] = pattern_hints
        if date_semantics:
            follow_state["task"] += " This is an ISO calendar date column; do not reinterpret a year condition as text matching."
        follow = client.call(follow_state, {"operator": {"type": "choice",
            "instructions": "Which of these two comparisons matches this column's condition?",
            "criteria": shortlist}})
        follow_answer = follow.answers["operator"]
        selected = follow_answer.get("choice")
        if selected not in shortlist:
            raise RuntimeError("Jev selected an invalid predicate operator refinement")
        calls += 1
        input_tokens += follow.input_tokens
        output_tokens += follow.output_tokens
        trace += ({"purpose": "refine close predicate operators", "state": follow_state,
                   "choices": shortlist, "selected": selected,
                   "probabilities": follow_answer.get("probabilities"),
                   "confidence": follow_answer.get("confidence"),
                   "input_tokens": follow.input_tokens,
                   "output_tokens": follow.output_tokens,
                   "elapsed_ms": follow.elapsed_ms},)
    if selected in ("CONTAINS", "PREFIX", "SUFFIX"):
        exact_values = tuple(value for value in value_hints if value.strip() and
                             re.search(r"(?<!\w)" + re.escape(value.strip()) + r"(?!\w)",
                                       request, re.IGNORECASE))
        if exact_values:
            # A proposed wildcard has an unresolved operand scope. Resolve
            # that scope once. Merely retrieving a text family never justifies
            # changing an established equality into a broader match.
            related_values = tuple(value for value in value_hints
                                   if value not in exact_values and
                                   any(exact.casefold() in value.casefold()
                                       for exact in exact_values))[:8]
            review_state = {"request": request, "condition_column": column.name,
                            "complete_observed_values_in_request": exact_values[:4],
                            "other_observed_values_containing_that_text": related_values,
                            "proposed_comparison": selected,
                            "task": "Decide only the operand scope. Related observed strings are candidates, not evidence that the user requested all of them."}
            review_choices = {
                "exact": "Identity/category comparison against the complete value or named entity; equality",
                "partial": "A text-content search explicitly matching a substring, prefix or suffix of the value",
                "ambiguous": "Cannot safely distinguish these meanings",
            }
            review = client.call(review_state, {"match_scope": {"type": "choice",
                "instructions": "Does this predicate require exact or partial matching, or is its scope ambiguous?",
                "criteria": review_choices}})
            scope = review.answers["match_scope"].get("choice")
            if scope not in review_choices:
                raise RuntimeError("Jev selected an invalid text match scope")
            if scope == "exact":
                selected = "="
            elif scope == "ambiguous":
                selected = "ambiguous"
            calls += 1
            input_tokens += review.input_tokens
            output_tokens += review.output_tokens
            trace += ({"purpose": "resolve proposed wildcard operand scope",
                       "state": review_state, "choices": review_choices, "selected": scope,
                       "probabilities": review.answers["match_scope"].get("probabilities"),
                       "input_tokens": review.input_tokens, "output_tokens": review.output_tokens,
                       "elapsed_ms": review.elapsed_ms},)
    if selected in {"none", "ambiguous"}:
        return PredicateOperator(selected, "ambiguous", "jev_unresolved", calls,
                                 input_tokens, output_tokens, trace)
    return PredicateOperator(selected, "resolved", "jev_choice", calls,
                             input_tokens, output_tokens, trace)


def resolve_predicate_column(request: str, table: str, columns: tuple[Column, ...],
                             client: JevClient | None = None,
                             group_column: str | None = None) -> PredicateColumn:
    """Select only the attribute whose value determines row qualification."""
    if not columns:
        return PredicateColumn(None, "ambiguous", "no_columns")
    if len(columns) == 1:
        return PredicateColumn(columns[0].name, "resolved", "only_column")
    client = client or JevClient()
    criteria = {f"c{index}": {"column": column.name,
                               "declared_type": column.declared_type}
                for index, column in enumerate(columns)}
    state = {"request": request, "source_table": table,
             "task": "Choose the WHERE attribute used to locate or qualify source rows. A value sought in the answer belongs to SELECT, not WHERE. Do not choose an output-only or sort-only field."}
    if group_column:
        state["group_key"] = group_column
        state["task"] += " The group key is only a WHERE attribute if the request restricts it to a specific value; merely grouping or displaying that column is not a source-row filter."
    questions = {"column": {"type": "choice",
                            "instructions": "Which column must be compared to locate the requested records?",
                            "criteria": criteria}}
    call = client.call(state, questions)
    answer = call.answers["column"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("Jev selected a column outside the inspected table")
    column = criteria[selected]["column"]
    trace = ({"purpose": "resolve predicate column", "state": state,
              "choices": criteria, "selected": column,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
    probabilities = answer.get("probabilities") or {}
    ranked = sorted(((key, float(probabilities.get(key) or 0)) for key in criteria),
                    key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < .25:
        shortlist = {key: criteria[key] for key, _ in ranked[:2]}
        follow_state = {"request": request, "source_table": table,
                        "group_key": group_column,
                        "task": "Choose the column with an explicit source-row value restriction. An output or group key without its own specified value is not a WHERE condition."}
        follow = client.call(follow_state, {"column": {"type": "choice",
            "instructions": "Which of these two columns actually restricts source rows?",
            "criteria": shortlist}})
        follow_answer = follow.answers["column"]
        selected = follow_answer.get("choice")
        if selected not in shortlist:
            raise RuntimeError("Jev selected an invalid predicate column refinement")
        column = criteria[selected]["column"]
        calls += 1
        input_tokens += follow.input_tokens
        output_tokens += follow.output_tokens
        trace += ({"purpose": "refine close predicate columns", "state": follow_state,
                   "choices": shortlist, "selected": column,
                   "probabilities": follow_answer.get("probabilities"),
                   "confidence": follow_answer.get("confidence"),
                   "input_tokens": follow.input_tokens,
                   "output_tokens": follow.output_tokens,
                   "elapsed_ms": follow.elapsed_ms},)
    return PredicateColumn(column, "resolved", "jev_choice", calls,
                           input_tokens, output_tokens, trace)
