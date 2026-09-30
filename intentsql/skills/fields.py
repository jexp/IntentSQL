"""Choose output columns from the selected table using bounded Jev judgments."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column
from intentsql.skills.output_order import resolve_output_order




def _entity_surface_forms(table: str) -> tuple[str, ...]:
    base = table.replace("_", " ").strip().casefold()
    forms = [base]
    if base.endswith("ies") and len(base) > 3:
        forms.append(base[:-3] + "y")
    elif base.endswith("s") and len(base) > 1:
        forms.append(base[:-1])
    return tuple(dict.fromkeys(forms))


def _explicit_entity_rows(request: str, table: str, columns: tuple[Column, ...]) -> bool:
    """Detect only strong surface evidence that the requested output is entities.

    This is deliberately conservative. An explicit later RETURN clause wins,
    and `show episode titles` is not treated as `show episodes` because the
    following word names a stored field.
    """
    if re.search(r"\breturn\b", request, re.I):
        return False
    column_forms = {column.name.replace("_", " ").casefold() for column in columns}
    expanded_forms = set(column_forms)
    for form in tuple(column_forms):
        if form.endswith("ies") and len(form) > 3:
            expanded_forms.add(form[:-3] + "y")
        elif form.endswith("s") and len(form) > 1:
            expanded_forms.add(form[:-1])
        elif form.endswith("y") and len(form) > 1:
            expanded_forms.add(form[:-1] + "ies")
        else:
            expanded_forms.add(form + "s")
    column_forms = expanded_forms
    for form in _entity_surface_forms(table):
        pattern = re.compile(
            r"^\s*(?:(?:please|kindly)\s+)?(?:show|list|find|display|get|give\s+me)\s+"
            r"(?:(?:all|every|the|some|matching)\s+)?" + re.escape(form) + r"\b",
            re.I)
        match = pattern.search(request)
        if not match:
            continue
        tail = request[match.end():].lstrip()
        # If the entity noun is immediately followed by a stored attribute
        # (singular or plural), the user is asking for that attribute rather
        # than complete entity rows: `show episode titles`, `list player names`.
        normalized_tail = tail.casefold().replace("_", " ")
        if any(re.match(r"^" + re.escape(form) + r"\b", normalized_tail)
               for form in sorted(column_forms, key=len, reverse=True)):
            continue
        return True
    return False

@dataclass(frozen=True)
class Fields:
    columns: tuple[str, ...]
    status: str  # resolved or ambiguous
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    all_columns: bool = False


def resolve_fields(request: str, table: str, columns: tuple[Column, ...],
                   client: JevClient | None = None) -> Fields:
    """The intent router has already decided output mode is specific fields."""
    if not columns:
        return Fields((), "ambiguous", "no_columns")
    if _explicit_entity_rows(request, table, columns):
        return Fields((), "resolved", "explicit_entity_rows", all_columns=True,
                      trace=({"purpose": "preserve explicit entity-row output",
                              "source_table": table},))
    if len(columns) == 1:
        return Fields((columns[0].name,), "resolved", "only_column")
    client = client or JevClient()
    candidates = {f"c{index}": column.name for index, column in enumerate(columns)}
    label_choices = {"none": "No implicit entity label is requested",
                     **{key: {"column": column.name,
                              "declared_type": column.declared_type}
                        for key, column in zip(candidates, columns)}}
    questions = {key: {"type": "noul",
                       "instructions": f"Should {name} be returned as an output column?"}
                 for key, name in candidates.items()}
    questions["entity_rows"] = {
        "type": "noul",
        "instructions": "Does the answer need a human-readable label for each source entity?",
    }
    questions["entity_label"] = {
        "type": "choice",
        "instructions": "Which column is the requested entity label, or none?",
        "criteria": label_choices,
    }
    questions["output_scope"] = {
        "type": "choice",
        "instructions": "Does the request return whole source records or specific returned attributes?",
        "criteria": {
            "all_rows": "Return the full source entities/records, with all their fields. Fields named only to filter or rank them do not narrow the output.",
            "selected_fields": "Return only the requested attributes/values of those records, possibly including a human-readable entity label.",
            "ambiguous": "The intended output scope is unclear.",
        },
    }
    state = {"request": request, "source_table": table,
             "candidate_output_columns": list(candidates.values()),
             "task": ("Select only fields the user asks to see. Do not return a field merely because it is used to filter or sort. "
                      "If the request names the table's entities as things to show (rather than asking only for one attribute's values), a human-readable entity label may also be an implicit output." )}
    call = client.call(state, questions)
    scores = {key: float(call.answers[key]["noul"]) for key in candidates}
    selected_keys = {key for key in candidates if scores[key] >= 0.6}
    entity_rows = float(call.answers["entity_rows"]["noul"])
    entity_label = call.answers["entity_label"].get("choice")
    output_scope = call.answers["output_scope"].get("choice")
    if output_scope not in questions["output_scope"]["criteria"]:
        raise RuntimeError("Jev selected an unknown output scope")
    all_columns = output_scope == "all_rows"
    if entity_label not in label_choices:
        raise RuntimeError("Jev selected an entity label outside inspected columns")
    if entity_rows >= 0.6 and entity_label != "none":
        selected_keys.add(entity_label)
    selected = tuple(name for key, name in candidates.items() if key in selected_keys)
    trace = ({"purpose": "resolve output fields", "state": state,
              "scores": {name: scores[key] for key, name in candidates.items()},
              "entity_rows": entity_rows,
              "output_scope": output_scope,
              "entity_label": None if entity_label == "none" else candidates[entity_label],
              "selected": selected, "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens, "elapsed_ms": call.elapsed_ms},)
    if all_columns:
        return Fields((), "resolved", "jev_full_records", 1,
                      call.input_tokens, call.output_tokens, trace, True)
    if output_scope == "ambiguous":
        return Fields((), "ambiguous", "jev_output_scope_ambiguous", 1,
                      call.input_tokens, call.output_tokens, trace)
    if not selected:
        return Fields((), "ambiguous", "jev_no_clear_field", 1,
                          call.input_tokens, call.output_tokens, trace)
    order = resolve_output_order(request, selected, client)
    return Fields(order.labels, "resolved", "jev_noul", 1 + order.jev_calls,
                  call.input_tokens + order.input_tokens,
                  call.output_tokens + order.output_tokens, trace + order.trace)


def resolve_distinct_target(request: str, table: str, columns: tuple[Column, ...],
                            client: JevClient | None = None) -> Fields:
    """COUNT DISTINCT needs exactly one target; use Choice, not Noul thresholds."""
    if not columns:
        return Fields((), "ambiguous", "no_columns")
    client = client or JevClient()
    candidates = {f"c{index}": column.name for index, column in enumerate(columns)}
    state = {"request": request, "source_table": table}
    call = client.call(state, {"target": {"type": "choice",
        "instructions": "Which column's distinct values should be counted?",
        "criteria": candidates}, "count_grain": {"type": "choice",
        "instructions": "Does the request establish unique attribute values or source records as the counting unit?",
        "criteria": {
            "distinct_values": "Explicitly count different/unique values of an attribute",
            "records": "Count source records/entities, not different attribute values",
            "ambiguous": "The requested counting unit is not established; do not assume uniqueness",
        }}})
    answer = call.answers["target"]
    selected = answer.get("choice")
    if selected not in candidates:
        raise RuntimeError("Jev selected a distinct target outside inspected columns")
    chosen = candidates[selected]
    grain = call.answers["count_grain"].get("choice")
    if grain not in {"distinct_values", "records", "ambiguous"}:
        raise RuntimeError("Jev selected an invalid counting unit")
    trace = ({"purpose": "resolve distinct count target", "state": state,
              "choices": candidates, "selected": chosen,
              "count_grain": grain,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return Fields((chosen,) if grain == "distinct_values" else (),
                  "resolved" if grain != "ambiguous" else "ambiguous",
                  "jev_row_count" if grain == "records" else "jev_choice", 1,
                  call.input_tokens, call.output_tokens, trace)
