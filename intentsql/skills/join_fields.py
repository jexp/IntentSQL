"""Select qualified output fields from two inspected related tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column, Relation
from intentsql.skills.output_order import resolve_output_order


@dataclass(frozen=True)
class QualifiedField:
    table: str
    column: str


@dataclass(frozen=True)
class JoinFields:
    fields: tuple[QualifiedField, ...]
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_join_fields(request: str, table_columns: dict[str, tuple[Column, ...]],
                        client: JevClient | None = None, *,
                        relation: Relation | None = None) -> JoinFields:
    fields = [QualifiedField(table, column.name)
              for table, columns in table_columns.items() for column in columns]
    if not fields:
        return JoinFields((), "ambiguous", "no_columns")
    client = client or JevClient()
    candidates = {f"f{index}": field for index, field in enumerate(fields)}
    state = {"request": request,
             "inspected_tables": {table: [column.name for column in columns]
                                  for table, columns in table_columns.items()},
             "task": (
                 "Select fields the user asks to display. A displayed field remains an output even "
                 "when also used to sort or filter. Fields mentioned only for sorting, filtering, "
                 "or joining are not outputs. When the user asks for a related entity itself rather "
                 "than its identifier, prefer a human-readable descriptive field from that related "
                 "table over a foreign-key ID; return an ID only when the request actually asks for it."
             )}
    if relation is not None:
        state["foreign_key"] = {
            "child_table": relation.child_table,
            "child_column": relation.child_column,
            "parent_table": relation.parent_table,
            "parent_column": relation.parent_column,
        }
    questions = {key: {"type": "noul",
                       "instructions": f"Should {field.table}.{field.column} be returned as an output field?"}
                 for key, field in candidates.items()}
    if relation is not None:
        label_choices = {
            "none": "No implicit human-readable entity label is requested",
            **{key: {"table": field.table, "column": field.column}
               for key, field in candidates.items()},
        }
        questions["entity_rows"] = {
            "type": "noul",
            "instructions": "Does the answer need a human-readable label from either joined entity?",
        }
        questions["entity_label"] = {
            "type": "choice",
            "instructions": "Which qualified field is the requested entity label, or none?",
            "criteria": label_choices,
        }
    call = client.call(state, questions)
    scores = {key: float(call.answers[key]["noul"]) for key in candidates}
    selected_keys = {key for key in candidates if scores[key] >= .6}
    entity_rows = None
    entity_label = None
    if relation is not None:
        entity_rows = float(call.answers["entity_rows"]["noul"])
        entity_label = call.answers["entity_label"].get("choice")
        if entity_label not in label_choices:
            raise RuntimeError("Jev selected an entity label outside inspected joined fields")
        if entity_rows >= .6 and entity_label != "none":
            selected_keys.add(entity_label)
    selected = tuple(field for key, field in candidates.items() if key in selected_keys)
    trace = ({"purpose": "resolve qualified output fields", "state": state,
              "scores": {f"{field.table}.{field.column}": scores[key]
                         for key, field in candidates.items()},
              "entity_rows": entity_rows,
              "entity_label": (None if entity_label in (None, "none") else
                               f"{candidates[entity_label].table}.{candidates[entity_label].column}"),
              "selected": [f"{item.table}.{item.column}" for item in selected],
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    if not selected:
        return JoinFields((), "ambiguous", "jev_no_clear_output", 1,
                          call.input_tokens, call.output_tokens, trace)
    labels = tuple(f"{field.table}.{field.column}" for field in selected)
    entity_field = (f"{candidates[entity_label].table}.{candidates[entity_label].column}"
                    if entity_rows is not None and entity_rows >= .6 and
                    entity_label not in (None, "none") else None)
    order = resolve_output_order(request, labels, client, entity_label=entity_field)
    by_label = {f"{field.table}.{field.column}": field for field in selected}
    ordered = tuple(by_label[label] for label in order.labels)
    return JoinFields(ordered, "resolved", "jev_noul", 1 + order.jev_calls,
                      call.input_tokens + order.input_tokens,
                      call.output_tokens + order.output_tokens, trace + order.trace)
