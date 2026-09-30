"""Choose one legal direct relationship generated from SQLite foreign keys."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Relation


@dataclass(frozen=True)
class RelatedTable:
    relation: Relation | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_related_table(request: str, base_table: str,
                          relations: tuple[Relation, ...],
                          client: JevClient | None = None,
                          related_columns: dict[str, tuple[str, ...]] | None = None) -> RelatedTable:
    choices = tuple(edge for edge in relations if base_table in
                    (edge.child_table, edge.parent_table))
    if not choices:
        return RelatedTable(None, "unsupported", "no_direct_foreign_key")
    if len(choices) == 1:
        return RelatedTable(choices[0], "resolved", "only_direct_relation")
    client = client or JevClient()
    criteria = {f"r{index}": {"related_table": edge.other_table(base_table),
                               **({"available_columns": related_columns.get(edge.other_table(base_table), ())}
                                  if related_columns is not None else {}),
                               "foreign_key": {"table": edge.child_table,
                                               "column": edge.child_column,
                                               "references": [edge.parent_table, edge.parent_column]}}
                for index, edge in enumerate(choices)}
    state = {"request": request, "selected_source_table": base_table}
    call = client.call(state, {"relation": {"type": "choice",
        "instructions": "Which inspected direct relation supplies the missing requested value?",
        "criteria": criteria}})
    answer = call.answers["relation"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("Jev selected a relationship outside inspected foreign keys")
    relation = choices[int(selected[1:])]
    trace = ({"purpose": "resolve one direct relationship", "state": state,
              "choices": criteria, "selected": criteria[selected],
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return RelatedTable(relation, "resolved", "jev_choice", 1,
                        call.input_tokens, call.output_tokens, trace)
