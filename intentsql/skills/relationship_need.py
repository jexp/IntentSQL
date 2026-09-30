"""Refine a routed relationship need after one table's columns are known."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class RelationshipNeed:
    needs_other_table: bool
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_relationship_need(request: str, table: str, columns: tuple[Column, ...],
                              client: JevClient | None = None, *,
                              related_schema: dict[str, Any] | None = None,
                              output_mode: str | None = None,
                              local_value_evidence: dict[str, tuple[str, ...]] | None = None) -> RelationshipNeed:
    client = client or JevClient()
    state = {"request": request, "selected_table": table,
             "available_columns": [column.name for column in columns]}
    if related_schema:
        state["inspected_direct_relationships"] = related_schema
    if output_mode:
        state["requested_output_mode"] = output_mode
    if local_value_evidence:
        state["targeted_local_value_evidence"] = local_value_evidence
    instructions = "Can the selected table answer the request, or is one listed direct relation required?"
    if related_schema:
        criteria = {
            "local_only": "The selected table contains every requested output, filter, and order value.",
            "direct_relation_required": "A requested value exists only in one listed directly related table.",
        }
        question = {"scope": {"type": "choice", "instructions": instructions,
                              "criteria": criteria}}
        call = client.call(state, question)
        answer = call.answers["scope"]
        selected = answer.get("choice")
        if selected not in criteria:
            raise RuntimeError("Jev selected an invalid relationship scope")
        needed = selected == "direct_relation_required"
        trace = ({"purpose": "refine relationship scope using inspected foreign keys",
                  "state": state, "choices": criteria, "selected": selected,
                  "probabilities": answer.get("probabilities"),
                  "confidence": answer.get("confidence"),
                  "input_tokens": call.input_tokens,
                  "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
        if needed:
            # A neighboring schema can lure the first choice into an unnecessary
            # join. Check sufficiency independently with only the chosen table;
            # a real join still wins when a requested human-facing fact is absent.
            local_state = {"request": request, "selected_table": table,
                           "available_columns": [column.name for column in columns],
                           "requested_output_mode": output_mode,
                           "task": "Verify whether these local columns cover the request."}
            if local_value_evidence:
                local_state["targeted_local_value_evidence"] = local_value_evidence
            local = client.call(local_state, {"local_sufficient": {"type": "noul",
                "instructions": "Do these columns cover every requested output, filter, and order value?"}})
            score = float(local.answers["local_sufficient"]["noul"])
            needed = score < .7
            calls += 1
            input_tokens += local.input_tokens
            output_tokens += local.output_tokens
            trace += ({"purpose": "verify a proposed join against local schema sufficiency",
                       "state": local_state, "score": score,
                       "selected": "local_only" if not needed else "direct_relation_required",
                       "input_tokens": local.input_tokens,
                       "output_tokens": local.output_tokens,
                       "elapsed_ms": local.elapsed_ms},)
        return RelationshipNeed(needed, "resolved", "jev_scope_review", calls,
                                input_tokens, output_tokens, trace)

    question = {"other_table": {"type": "noul", "instructions": instructions}}
    call = client.call(state, question)
    answer = call.answers["other_table"]
    score = float(answer["noul"])
    trace = ({"purpose": "refine relationship need using selected table schema",
              "state": state, "score": score,
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return RelationshipNeed(score >= .6, "resolved", "jev_noul", 1,
                            call.input_tokens, call.output_tokens, trace)
