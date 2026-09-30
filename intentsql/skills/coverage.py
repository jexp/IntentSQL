"""Check that a compiled read plan retains the request's material semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient


_COVERAGE = {
    "complete": "The plan fully preserves every material requirement in the request",
    "missing_output": "A requested result field or value is absent from the plan",
    "missing_filter": "A requested source-row qualification is absent or wrong",
    "missing_order_limit": "A requested ranking, tie order, or result count is absent or wrong",
    "unsupported_relation": "The request needs more or different table relationships, a self join, or a comparison between rows",
    "unsupported_expression": "The request needs a computed/conditional output or subquery not represented in the plan",
    "unsupported_distinct": "The request requires distinctness over more values than the plan counts or returns",
    "unsupported_boolean": "The request needs Boolean grouping or negation not represented in the plan",
}


def _mentioned_schema_columns(request: str,
                              source_columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return schema identifiers whose component words occur in the request.

    This is lexical evidence only. It does not assign a semantic role or infer
    a column from a phrase.
    """
    words = re.findall(r"[a-z0-9]+", request.casefold())
    mentioned: list[str] = []
    for column in source_columns:
        parts = re.findall(r"[a-z0-9]+", column.casefold())
        if parts and any(words[index:index + len(parts)] == parts
                         for index in range(len(words) - len(parts) + 1)):
            mentioned.append(column)
    return tuple(mentioned)


@dataclass(frozen=True)
class Coverage:
    category: str
    status: str
    source: str = "jev_choice"
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def review_unique_result_filter(request: str, program: dict[str, Any],
                                full_row: dict[str, Any],
                                client: JevClient) -> Coverage:
    """Recheck only a suspected missing filter against one mechanically unique row."""
    state = {
        "request": request,
        "compiled_filters": program.get("filters") or [],
        "unique_matching_source_row": full_row,
        "task": (
            "The compiled predicates mechanically match exactly one source row. Decide whether "
            "the request contains another independent stored-value restriction absent from the "
            "filters. A descriptive phrase that identifies or describes this unique row is not "
            "automatically another predicate. Require a missing filter only when the request "
            "actually constrains a separate stored attribute or value."
        ),
    }
    criteria = {
        "complete": (
            "The unmatched wording is descriptive/contextual or is already satisfied by the "
            "unique row and compiled predicate; no separate stored column comparison is stated."
        ),
        "missing_filter": (
            "The request explicitly constrains another stored attribute/value independently of "
            "the compiled predicates, and the unique row does not remove that requirement."
        ),
    }
    call = client.call(state, {"filter_coverage": {
        "type": "choice",
        "instructions": "Is one independent stored-value filter still missing?",
        "criteria": criteria,
    }})
    selected = call.answers["filter_coverage"].get("choice")
    if selected not in criteria:
        raise RuntimeError("System One selected an invalid filter coverage result")
    trace = ({"purpose": "review suspected missing filter against unique matched row",
              "state": state, "choices": criteria, "selected": selected,
              "probabilities": call.answers["filter_coverage"].get("probabilities"),
              "confidence": call.answers["filter_coverage"].get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return Coverage(selected, "resolved" if selected == "complete" else "unsupported",
                    jev_calls=1, input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens, trace=trace)


def resolve_coverage(request: str, program: dict[str, Any],
                     client: JevClient, *,
                     source_columns: tuple[str, ...] = ()) -> Coverage:
    """One bounded decision; code rejects any uncovered requirement."""
    plan = {key: program.get(key) for key in (
        "joined_tables", "outputs", "filters", "filter_connector", "groups",
        "having", "order_by", "limit", "distinct", "per_group_extremum",
        "relationship_scope")}
    if program.get("global_extremum") is not None:
        plan["global_extremum"] = program["global_extremum"]
    source_invariants = program.get("source_invariants") or {}
    if source_invariants:
        plan["source_invariants"] = source_invariants
    grounded_patterns = program.get("grounded_pattern_bindings") or []
    if grounded_patterns:
        plan["grounded_pattern_bindings"] = grounded_patterns
    grounded_values = program.get("grounded_value_bindings") or []
    if grounded_values:
        plan["grounded_value_bindings"] = grounded_values
    state = {
        "request": request,
        "compiled_plan": plan,
        "ordering_semantics": (
            "ASC/DESC apply to stored values. A semantic rank can be inverse to its label; "
            "for example, a later birth value correctly ranks a younger entity first."),
        "task": ("Check semantic coverage. Treat every requested output, filter, "
                 "relationship, grouping, ordering, and limit as mandatory. If any "
                 "requirement is missing or needs an operation outside this plan, choose "
                 "the most decisive missing category. SQL validity alone is insufficient."),
    }
    questions = {"coverage": {"type": "choice",
                              "instructions": "Does this exact compiled plan satisfy the entire user request?",
                              "criteria": _COVERAGE}}
    call = client.call(state, questions)
    answer = call.answers["coverage"]
    selected = answer.get("choice")
    if selected not in _COVERAGE:
        raise RuntimeError("System One selected an invalid coverage category")
    trace: tuple[dict[str, Any], ...] = ({"purpose": "check request coverage before execution", "state": state,
              "choices": _COVERAGE, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
    if selected == "unsupported_expression":
        pattern_filters = [item for item in (plan.get("filters") or [])
                           if item.get("operator") in {"CONTAINS", "PREFIX", "SUFFIX"}]
        if pattern_filters:
            # LIKE-style text predicates are first-class typed filters in the
            # compiler, not derived output expressions.  Recheck only the
            # coverage objection instead of letting the broad final question
            # reinterpret a represented PREFIX/SUFFIX/CONTAINS as unsupported.
            pattern_state = {
                "request": request,
                "typed_pattern_filters": pattern_filters,
                "outputs": plan.get("outputs"),
                "task": ("The listed CONTAINS/PREFIX/SUFFIX operations are fully represented "
                         "stored-value row predicates. Decide only whether the request still "
                         "requires some separate computed/conditional output expression."),
            }
            review = client.call(pattern_state, {"expression_coverage": {
                "type": "choice",
                "instructions": "Is a separate unsupported expression still required?",
                "criteria": {
                    "complete": "No; the text operation is already represented as a row predicate.",
                    "unsupported_expression": "Yes; another requested computed/conditional expression is absent.",
                },
            }})
            revised = review.answers["expression_coverage"].get("choice")
            if revised not in {"complete", "unsupported_expression"}:
                raise RuntimeError("System One selected an invalid expression coverage review")
            calls += 1
            input_tokens += review.input_tokens
            output_tokens += review.output_tokens
            trace += ({"purpose": "review represented text predicate versus expression",
                       "state": pattern_state, "selected": revised,
                       "probabilities": review.answers["expression_coverage"].get("probabilities"),
                       "confidence": review.answers["expression_coverage"].get("confidence"),
                       "input_tokens": review.input_tokens,
                       "output_tokens": review.output_tokens,
                       "elapsed_ms": review.elapsed_ms},)
            selected = revised
    if selected == "missing_filter" and plan.get("global_extremum") and not plan["filters"]:
        unit_match = re.search(r"\b(?:for\s+each|per)\s+[A-Za-z_]+", request, re.I)
        filter_state = {
            "request": request,
            "established_output": plan["outputs"],
            "established_global_extremum": plan["global_extremum"],
            "established_ordering": plan["order_by"],
            "inspected_source_columns": source_columns,
            "unit_phrase": unit_match.group(0) if unit_match else None,
            "task": ("Check only for an independent stored-row condition. A phrase describing "
                     "the selected metric or its unit is already represented by the extremum and "
                     "does not itself require WHERE."),
        }
        questions = {"missing_filter": {"type": "noul",
            "instructions": "Is an independent stored-value row restriction still missing?"}}
        if unit_match:
            questions["unit_matches_metric"] = {"type": "noul",
                "instructions": (f"Does '{unit_match.group(0)}' describe the unit of the inspected "
                                 f"{plan['global_extremum']['column']} metric?")}
        review = client.call(filter_state, questions)
        score = float(review.answers["missing_filter"].get("noul") or 0)
        unit_score = (float(review.answers["unit_matches_metric"].get("noul") or 0)
                      if unit_match else 0)
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review missing filter for grounded global extremum",
                   "state": filter_state, "score": score,
                   "unit_matches_metric": unit_score,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if score <= .25 or (unit_match and score < .5 and unit_score >= .75):
            selected = "complete"
    if (selected == "missing_filter" and not plan.get("filters") and
            plan.get("groups")):
        # A broad whole-request coverage judgment can confuse a GROUP BY term
        # (for example, "each school type") with a missing WHERE condition.
        # Recheck only that narrow objection while treating the already-typed
        # grouping/measure/ordering as established facts. A genuine qualifier
        # such as "public schools by city" must still remain missing_filter.
        grouped_filter_state = {
            "request": request,
            "established_groups": plan.get("groups") or [],
            "established_outputs": plan.get("outputs") or [],
            "established_having": plan.get("having") or [],
            "established_order_by": plan.get("order_by") or [],
            "task": (
                "Check only for an independent source-row restriction that belongs in WHERE. "
                "Words that name a grouping key, computed measure, output label, or ordering "
                "do not themselves imply a row filter."),
        }
        grouped_review = client.call(grouped_filter_state, {"grouped_filter_coverage": {
            "type": "choice",
            "instructions": "Is a separate source-row WHERE condition still missing?",
            "criteria": {
                "complete": (
                    "No. The request only describes grouping, outputs, aggregate measure, "
                    "HAVING, or ordering beyond the compiled plan."),
                "missing_filter": (
                    "Yes. The request independently restricts which source rows qualify, "
                    "and that restriction is absent from WHERE."),
            },
        }})
        revised = grouped_review.answers["grouped_filter_coverage"].get("choice")
        if revised not in {"complete", "missing_filter"}:
            raise RuntimeError("System One selected an invalid grouped filter coverage review")
        calls += 1
        input_tokens += grouped_review.input_tokens
        output_tokens += grouped_review.output_tokens
        trace += ({"purpose": "review suspected grouped row filter against established group semantics",
                   "state": grouped_filter_state, "selected": revised,
                   "probabilities": grouped_review.answers["grouped_filter_coverage"].get("probabilities"),
                   "confidence": grouped_review.answers["grouped_filter_coverage"].get("confidence"),
                   "input_tokens": grouped_review.input_tokens,
                   "output_tokens": grouped_review.output_tokens,
                   "elapsed_ms": grouped_review.elapsed_ms},)
        selected = revised

    if selected == "missing_filter" and source_invariants:
        invariant_state = {
            "request": request,
            "compiled_filters": plan["filters"],
            "mechanically_constant_source_values": source_invariants,
            "task": ("A constant source value is true for every row and therefore need not be emitted as a SQL WHERE clause. Decide only whether every allegedly missing row qualifier is already guaranteed by these inspected invariants."),
        }
        review = client.call(invariant_state, {"invariants_cover": {"type": "noul",
            "instructions": "Do these source invariants cover the suspected missing filter?"}})
        score = float(review.answers["invariants_cover"].get("noul") or 0)
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review suspected filter against constant source values",
                   "state": invariant_state, "score": score,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if score >= .6:
            selected = "complete"
    if selected == "missing_filter" and grounded_patterns:
        pattern_state = {
            "request": request,
            "compiled_filters": plan["filters"],
            "grounded_pattern_bindings": grounded_patterns,
            "task": ("Each pattern operand is a recurring stored annotation and was independently bound to the request meaning. Decide whether the compiled pattern filter covers the requested row qualification without omitting another condition."),
        }
        review = client.call(pattern_state, {"pattern_complete": {"type": "noul",
            "instructions": "Does this grounded stored-pattern predicate fully cover the request's row condition?"}})
        score = float(review.answers["pattern_complete"].get("noul") or 0)
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review filter coverage against grounded stored pattern",
                   "state": pattern_state, "score": score,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if score >= .6:
            selected = "complete"
    if selected == "missing_filter" and grounded_values:
        review_state = {
            "request": request,
            "compiled_filters": plan["filters"],
            "grounded_value_bindings": grounded_values,
            "task": "The listed stored values were independently selected from inspected database candidates. Check only whether a separate source-row condition is still absent.",
        }
        review = client.call(review_state, {"missing_filter": {"type": "noul",
            "instructions": "Beyond these bound predicates, is another independent row filter missing?"}})
        score = float(review.answers["missing_filter"].get("noul") or 0)
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review filter coverage with grounded value bindings",
                   "state": review_state, "score": score,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if score <= .3:
            selected = "complete"
    if (selected == "missing_order_limit" and not plan.get("global_extremum")
            and (plan.get("order_by") or plan.get("limit") is not None)):
        order_state = {
            "request": request,
            "established_order_by": plan.get("order_by") or [],
            "established_limit": plan.get("limit"),
            "task": (
                "The listed ordering keys, stored-value directions, and row limit are already "
                "resolved typed facts. Do not reinterpret whether those established facts were "
                "the right semantic choices. Check only whether the request explicitly asks for "
                "an additional ordering key, tie order, or result-count constraint that is absent."),
        }
        review = client.call(order_state, {"order_limit_coverage": {
            "type": "choice",
            "instructions": "Is an additional explicit ordering or result-count requirement absent?",
            "criteria": {
                "complete": "No; the established ORDER BY keys/directions and LIMIT cover the request.",
                "missing_order_limit": "Yes; another explicit ordering key, tie order, or count is still absent.",
            },
        }})
        revised = review.answers["order_limit_coverage"].get("choice")
        if revised not in {"complete", "missing_order_limit"}:
            raise RuntimeError("System One selected an invalid order/limit coverage review")
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review suspected missing order/limit against established typed facts",
                   "state": order_state, "selected": revised,
                   "probabilities": review.answers["order_limit_coverage"].get("probabilities"),
                   "confidence": review.answers["order_limit_coverage"].get("confidence"),
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        selected = revised
    if selected == "missing_order_limit" and plan.get("global_extremum"):
        extremum_state = {
            "request": request,
            "global_extremum": plan["global_extremum"],
            "order_by": plan["order_by"],
            "limit": plan["limit"],
            "task": ("A global MIN/MAX row selector returns every tied extremal row without LIMIT. An implicit singular question about the highest or lowest value is covered by a tie-preserving selector; an explicit top-N count greater than one is not. Decide whether this represents the requested cardinality and ordering semantics."),
        }
        review = client.call(extremum_state, {"extremum_complete": {"type": "noul",
            "instructions": "Does this tie-preserving extremum satisfy the requested result count?"}})
        score = float(review.answers["extremum_complete"].get("noul") or 0)
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review order/limit against tie-preserving global extremum",
                   "state": extremum_state, "score": score,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if score >= .6:
            selected = "complete"
    if selected == "missing_output":
        # Require the suspected omission to identify one concrete inspected
        # column. This prevents a vague second judgment from contradicting a
        # projection that has already selected every requested field.
        returned_leaf_names = {
            str(value).rsplit(".", 1)[-1]
            for value in (plan["outputs"] or [])
        }
        absent_columns = tuple(
            column for column in source_columns
            if column not in returned_leaf_names)
        review_state = {
            "request": request,
            "returned_values": plan["outputs"],
            "non_output_uses": {
                "filters": plan["filters"],
                "ordering": plan["order_by"],
                "grouping": plan["groups"],
            },
            "tables": plan["joined_tables"],
            "explicit_schema_mentions": _mentioned_schema_columns(
                request, source_columns),
            "per_group_row_selector": plan.get("per_group_extremum"),
            "schema_note": (
                "A source entity word identifies row grain; it is not an extra output. "
                "A field mentioned only to filter, group, or order rows is not automatically "
                "a requested output. "
                "Each named output returns that stored value, even when the request uses "
                "a longer description."),
        }
        output_criteria = {
            "none": "No inspected source column requested for output is missing.",
            **{f"c{index}": {"missing_column": column}
               for index, column in enumerate(absent_columns)},
        }
        review_questions = {"output_coverage": {"type": "choice",
            "instructions": "Which absent source column did the user ask to return?",
            "criteria": output_criteria}}
        review = client.call(review_state, review_questions)
        output_review = review.answers["output_coverage"].get("choice")
        if output_review not in output_criteria:
            raise RuntimeError("System One selected an invalid output coverage result")
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review suspected missing output", "state": review_state,
                   "choices": output_criteria, "selected": output_review,
                   "probabilities": review.answers["output_coverage"].get("probabilities"),
                   "confidence": review.answers["output_coverage"].get("confidence"),
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if output_review == "none":
            remaining = {key: value for key, value in _COVERAGE.items()
                         if key != "missing_output"}
            follow_state = {**state, "output_review": (
                "A separate bounded check confirmed the returned fields cover the requested "
                "output. Check every other requirement.")}
            follow = client.call(follow_state, {"coverage": {"type": "choice",
                "instructions": "What other material request requirement is missing from this plan, if any?",
                "criteria": remaining}})
            revised = follow.answers["coverage"].get("choice")
            if revised not in remaining:
                raise RuntimeError("System One selected an invalid remaining coverage category")
            selected = revised
            calls += 1
            input_tokens += follow.input_tokens
            output_tokens += follow.output_tokens
            trace += ({"purpose": "check remaining request coverage", "state": follow_state,
                       "choices": remaining, "selected": selected,
                       "probabilities": follow.answers["coverage"].get("probabilities"),
                       "confidence": follow.answers["coverage"].get("confidence"),
                       "input_tokens": follow.input_tokens,
                       "output_tokens": follow.output_tokens,
                       "elapsed_ms": follow.elapsed_ms},)
    if selected == "unsupported_relation" and len(plan["joined_tables"] or []) == 1 and source_columns:
        # The broad coverage choice can over-read entity words as a join.
        # Recheck that exact objection with only the compiled source and its
        # inspected columns; a true missing relation still fails closed.
        local_state = {"request": request, "source_table": plan["joined_tables"][0],
                       "source_columns": source_columns, "compiled_plan": plan}
        criteria = {
            "local_complete": "Every requested fact is represented by this table and plan.",
            "missing_relation": "A requested fact requires another table.",
        }
        review = client.call(local_state, {"relationship_coverage": {"type": "choice",
            "instructions": "Is a fact absent from this local plan?", "criteria": criteria}})
        selected_review = review.answers["relationship_coverage"].get("choice")
        if selected_review not in criteria:
            raise RuntimeError("System One selected an invalid relationship coverage result")
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review suspected missing relationship against local schema",
                   "state": local_state, "selected": selected_review,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if selected_review == "local_complete":
            selected = "complete"
    elif (selected == "unsupported_relation" and len(plan["joined_tables"] or []) == 2
          and len(program.get("joins") or []) == 1):
        direct_state = {
            "request": request,
            "validated_direct_join": program["joins"][0],
            "joined_tables": plan["joined_tables"],
            "outputs": plan["outputs"],
            "filters": plan["filters"],
            "order_by": plan["order_by"],
        }
        review = client.call(direct_state, {"direct_join_complete": {"type": "noul",
            "instructions": "Does this validated direct join satisfy the complete request?"}})
        score = float(review.answers["direct_join_complete"].get("noul") or 0)
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "review suspected unsupported relation against direct join",
                   "state": direct_state, "score": score,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if score >= .5:
            selected = "complete"
    return Coverage(selected, "resolved" if selected == "complete" else "unsupported",
                    jev_calls=calls, input_tokens=input_tokens,
                    output_tokens=output_tokens, trace=trace)
