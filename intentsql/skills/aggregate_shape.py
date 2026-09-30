"""Refine whether an aggregate is scalar or computed independently per group."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class AggregateShape:
    shape: str
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_aggregate_shape(request: str, table: str, columns: tuple[Column, ...],
                            client: JevClient | None = None) -> AggregateShape:
    """Distinguish scalar aggregation from grouped aggregation before picking the function.

    A global extremum over already-computed group measures is represented as
    GROUP BY + aggregate + ORDER BY + LIMIT, not as a nested scalar aggregate.
    """
    client = client or JevClient()
    criteria = {
        "scalar": (
            "Compute one SUM/AVG/MIN/MAX over all qualifying source rows; there is no "
            "separate aggregate value per category/group."
        ),
        "grouped": (
            "Compute an aggregate separately for each group/category. This also includes "
            "asking which group has the highest/lowest/best/worst group aggregate: compute "
            "the per-group measure first, then rank/select groups."
        ),
        "row_extremum": (
            "Return stored fields from the source row or related entity whose existing numeric/date "
            "attribute is globally smallest/largest. The MIN/MAX chooses qualifying row(s), and the "
            "aggregate value itself is not the requested scalar output. Preserve ties."
        ),
        "unsupported_nested": (
            "Requires a true aggregate over aggregate results that cannot be expressed as "
            "ordinary GROUP BY followed only by ordering/limiting the grouped rows."
        ),
    }
    state = {
        "request": request,
        "source_table": table,
        "available_columns": [column.name for column in columns],
        "task": (
            "Choose the relational result shape before choosing the aggregate function. "
            "Do not mistake highest/lowest selection among grouped aggregate rows for a nested "
            "aggregate: selecting the group with the smallest yearly maximum is grouped."
        ),
    }
    call = client.call(state, {
        "shape": {
            "type": "choice",
            "instructions": "Which supported aggregate result shape does the request need?",
            "criteria": criteria,
        },
        "stored_row_extremum": {
            "type": "noul",
            "instructions": "Does a global stored-value extremum select the returned source row or entity?",
        },
    })
    answer = call.answers["shape"]
    shape = answer.get("choice")
    if shape not in criteria:
        raise RuntimeError("Jev selected an invalid aggregate shape")
    row_score = float(call.answers["stored_row_extremum"].get("noul") or 0)
    if row_score >= .6:
        shape = "row_extremum"
    trace = ({
        "purpose": "refine aggregate result shape",
        "state": state,
        "choices": criteria,
        "selected": shape,
        "stored_row_extremum_score": row_score,
        "probabilities": answer.get("probabilities"),
        "confidence": answer.get("confidence"),
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    return AggregateShape(shape, "unsupported" if shape == "unsupported_nested" else "resolved",
                          "jev_choice", 1, call.input_tokens, call.output_tokens, trace)
