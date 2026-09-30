"""Choose a bounded aggregate function and schema-grounded target."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column


FUNCTIONS = {
    "COUNT": "number of source records or values",
    "AVG": "arithmetic mean of numeric values",
    "SUM": "total of numeric values",
    "MIN": "smallest value",
    "MAX": "largest value",
    "unsupported": "not expressible as one scalar aggregate of one source column",
}


@dataclass(frozen=True)
class Aggregate:
    function: str | None
    column: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_aggregate(request: str, table: str, columns: tuple[Column, ...],
                      client: JevClient | None = None, *,
                      group_key: str | None = None) -> Aggregate:
    if not columns:
        return Aggregate(None, None, "ambiguous", "no_columns")
    client = client or JevClient()
    targets = {f"c{index}": {"column": column.name,
                               "declared_type": column.declared_type}
               for index, column in enumerate(columns)}
    state = {"request": request, "source_table": table}
    if group_key is not None:
        state["group_key"] = group_key
        state["task"] = (
            "Choose the aggregate computed inside each group before any ordering or top/bottom "
            "selection across groups. Words such as highest/lowest may describe ranking of the "
            "finished group rows rather than a different aggregate function."
        )
    questions = {
        "function": {"type": "choice",
                     "instructions": (
                         "Which aggregate operation computes the per-group measure? "
                         "Ignore any later ranking of groups." if group_key is not None else
                         "Which scalar aggregate operation answers the request?"
                     ),
                     "criteria": FUNCTIONS},
        "target": {"type": "choice",
                   "instructions": "Which inspected column supplies the aggregate values?",
                   "criteria": targets},
        "extremum": {"type": "choice",
                     "instructions": ("Which stored-value extreme computes the measure itself? "
                                      "Ignore later group ordering. Position/rank may improve as "
                                      "the stored number decreases; do not confuse best with largest."),
                     "criteria": {"MIN": "smallest stored value", "MAX": "largest stored value",
                                  "neither": "the measure is not an extremum",
                                  "ambiguous": "stored-value direction cannot be established"}},
    }
    call = client.call(state, questions)
    function = call.answers["function"].get("choice")
    target = call.answers["target"].get("choice")
    if function not in FUNCTIONS or target not in targets:
        raise RuntimeError("Jev selected aggregate outside supplied choices")
    column = targets[target]["column"]
    extremum = call.answers["extremum"].get("choice")
    if extremum not in questions["extremum"]["criteria"]:
        raise RuntimeError("Jev selected an invalid aggregate direction")
    status = "unsupported" if function == "unsupported" else "resolved"
    if function in ("MIN", "MAX") and extremum != function:
        status = "ambiguous"
    trace = ({"purpose": "resolve scalar aggregate", "state": state,
              "choices": {"function": FUNCTIONS, "target": targets},
              "selected": {"function": function, "column": column},
              "extremum_review": extremum,
              "probabilities": {key: call.answers[key].get("probabilities") for key in questions},
              "confidence": {key: call.answers[key].get("confidence") for key in questions},
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return Aggregate(function, column, status,
                     "jev_choice", 1,
                     call.input_tokens, call.output_tokens, trace)
