"""Resolve a routed group hint when the request actually asks for row fields."""

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class FieldShape:
    shape: str
    distinct: bool
    status: str
    source: str = "jev_choice"
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_field_shape(request: str, table: str,
                        reachable: dict[str, tuple[Column, ...]],
                        distinct_hint: bool, client: JevClient,
                        stored_evidence: dict[str, Any] | None = None) -> FieldShape:
    choices = {
        "rows": "Return existing row attributes, possibly filtered or sorted.",
        "groups": "Compute a new COUNT/SUM/AVG/MIN/MAX across multiple rows per group.",
        "global_extremum": "Return row fields having the global minimum or maximum stored value, preserving ties.",
        "ambiguous": "The requested result shape cannot be determined safely.",
    }
    questions: dict[str, Any] = {"shape": {
        "type": "choice", "instructions": "Does the answer need stored rows, computed groups, or rows at a global extremum?",
        "criteria": choices}}
    if distinct_hint:
        questions["distinct"] = {"type": "noul",
                                  "instructions": "Does the request remove duplicate output values?"}
    state = {"request": request, "source_table": table,
             "reachable_columns": {name: tuple({"column": col.name,
                 "type": col.declared_type, "primary_key": col.primary_key} for col in columns)
                 for name, columns in reachable.items()},
             "stored_evidence": stored_evidence or {},
             "task": "One stored row per entity is ROWS. GROUPS must combine multiple source rows into a new measure."}
    call = client.call(state, questions)
    answer = call.answers["shape"]
    shape = answer.get("choice")
    if shape not in choices:
        raise RuntimeError("Jev selected an invalid field result shape")
    distinct = (float(call.answers["distinct"]["noul"]) >= .6
                if distinct_hint else False)
    trace = ({"purpose": "resolve field result shape", "state": state,
              "choices": choices, "selected": shape,
              "distinct": distinct,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return FieldShape(shape, distinct,
                      "resolved" if shape != "ambiguous" else "ambiguous",
                      jev_calls=1, input_tokens=call.input_tokens,
                      output_tokens=call.output_tokens, trace=trace)
