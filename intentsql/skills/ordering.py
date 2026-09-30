"""Resolve one ordering term from inspected columns and bounded directions."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column




def _column_surface_pattern(name: str) -> str:
    parts = [re.escape(part) for part in re.split(r"[_\s-]+", name) if part]
    return r"[\s_-]+".join(parts)




def _direction_from_local_tail(tail: str) -> str:
    if re.search(r"\b(?:desc(?:ending)?|highest|largest|greatest|latest|newest|z\s*(?:to|-)\s*a)\b", tail, re.I):
        return "DESC"
    if re.search(r"\b(?:asc(?:ending)?|lowest|smallest|earliest|oldest|alphabetic(?:al(?:ly)?)?|a\s*(?:to|-)\s*z)\b", tail, re.I):
        return "ASC"
    return "ASC"


def _best_explicit_match(request: str, columns: tuple[Column, ...],
                         pattern: str, primary: str | None = None) -> tuple[str, str] | None:
    """Pick the longest explicitly named column in one ordering clause."""
    found: list[tuple[int, int, str, str]] = []
    for column in columns:
        if primary and column.name == primary:
            continue
        surface = _column_surface_pattern(column.name)
        match = re.search(pattern + r"(?P<column>" + surface + r")(?P<tail>[^,.;]*)",
                          request, re.I)
        if not match:
            continue
        tail = match.group("tail")
        primary_tail = re.split(r"\bthen\b", tail, maxsplit=1, flags=re.I)[0]
        if re.search(r"\band\b", primary_tail, re.I):
            continue
        found.append((len(match.group("column")), match.start(), column.name,
                      _direction_from_local_tail(primary_tail)))
    if not found:
        return None
    found.sort(key=lambda item: (-item[0], item[1]))
    return found[0][2], found[0][3]


def _explicit_secondary_order_clause(request: str, columns: tuple[Column, ...],
                                     primary: str | None = None) -> tuple[str, str] | None:
    """Resolve an explicitly named `then <column>` tie order mechanically."""
    return _best_explicit_match(
        request, columns, r"\bthen\s+(?:by\s+)?(?:the\s+)?", primary)


def _explicit_order_clause(request: str, columns: tuple[Column, ...]) -> tuple[str, str] | None:
    """Resolve an explicit `order/sort by <column>` clause mechanically.

    Direction words are read only from the text before a `then` tie-break, so
    a secondary direction cannot leak into the primary key. `and` inside the
    primary clause stays with the bounded semantic resolver. A comma is not
    required between the two keys.
    """
    return _best_explicit_match(
        request, columns, r"\b(?:order(?:ed)?|sort(?:ed)?)\s+by\s+(?:the\s+)?")

@dataclass(frozen=True)
class Ordering:
    column: str | None
    direction: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    secondary_column: str | None = None
    secondary_direction: str | None = None


@dataclass(frozen=True)
class OrderNeed:
    needed: bool
    source: str = "jev_noul"
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def review_order_direction(request: str, table: str, column: Column,
                           current: str, client: JevClient) -> Ordering:
    """Recheck only stored-value direction after a typed order fails coverage."""
    if current not in {"ASC", "DESC"}:
        raise ValueError("Invalid established ordering direction")
    state = {"request": request,
             "established_order_column": f"{table}.{column.name}",
             "declared_type": column.declared_type,
             "current_direction": current}
    choices = {"ASC": "Smaller stored values first",
               "DESC": "Larger stored values first",
               "ambiguous": "The requested rank cannot be determined from this column"}
    call = client.call(state, {"direction": {"type": "choice",
        "instructions": "Which direction on this established stored column yields the requested first row? An age value grows older; a birth date grows younger.",
        "criteria": choices}})
    selected = call.answers["direction"].get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected an invalid ordering direction")
    trace = ({"purpose": "review direction on established ordering column",
              "state": state, "choices": choices, "selected": selected,
              "probabilities": call.answers["direction"].get("probabilities"),
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return Ordering(column.name, selected if selected != "ambiguous" else None,
                    "resolved" if selected != "ambiguous" else "ambiguous",
                    "jev_direction_review", 1, call.input_tokens,
                    call.output_tokens, trace)


def resolve_single_row_order_need(request: str, client: JevClient) -> OrderNeed:
    """Separate a global extremum from an arbitrary one-row request."""
    state = {"request": request, "result_count": 1}
    questions = {"requires_order": {"type": "noul",
        "instructions": "Does selecting this one row require a requested ranking or extremum?"}}
    call = client.call(state, questions)
    score = float(call.answers["requires_order"].get("noul") or 0)
    trace = ({"purpose": "check single-row ordering requirement", "state": state,
              "selected": score, "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens, "elapsed_ms": call.elapsed_ms},)
    return OrderNeed(score >= .6, jev_calls=1, input_tokens=call.input_tokens,
                     output_tokens=call.output_tokens, trace=trace)


def resolve_ordering(request: str, table: str, columns: tuple[Column, ...],
                     client: JevClient | None = None) -> Ordering:
    """One call decides target and direction; the graph invokes it only if routed."""
    if not columns:
        return Ordering(None, None, "ambiguous", "no_columns")
    explicit = _explicit_order_clause(request, columns)
    if explicit is not None:
        column, direction = explicit
        secondary = _explicit_secondary_order_clause(request, columns, column)
        return Ordering(column, direction, "resolved", "explicit_order_clause",
                        trace=({"purpose": "resolve explicit ordering clause",
                                "selected": {"column": column, "direction": direction,
                                             "secondary_column": secondary[0] if secondary else None,
                                             "secondary_direction": secondary[1] if secondary else None}},),
                        secondary_column=secondary[0] if secondary else None,
                        secondary_direction=secondary[1] if secondary else None)
    client = client or JevClient()
    choices = {f"c{index}": {"column": column.name,
                              "declared_type": column.declared_type}
               for index, column in enumerate(columns)}
    target_choices = {
        **choices,
        "none": "No inspected column grounds the requested ranking or ordering concept.",
    }
    state = {"request": request, "source_table": table}
    directions = {"ASC": "smaller stored values first; early dates; A to Z",
                  "DESC": "larger stored values first; late dates; Z to A"}
    secondary_choices = {"none": "No explicitly requested secondary ordering term", **choices}
    questions = {
        "target": {"type": "choice",
                   "instructions": ("Which inspected column is actually named or semantically grounded as "
                                    "the primary ordering key? Choose none when the ranking concept has no "
                                    "grounded database attribute."),
                   "criteria": target_choices},
        "direction": {"type": "choice", "instructions": (
                          "Which stored-value direction produces the requested ranking? "
                          "Use the selected column's meaning; for example, a later birth value means younger."),
                      "criteria": directions},
        "direction_explicit": {"type": "noul",
                               "instructions": "Does the request determine the primary sort direction?"},
        "secondary_target": {"type": "choice",
                             "instructions": "Which explicit second sort key is requested, or none?",
                             "criteria": secondary_choices},
        "secondary_direction": {"type": "choice",
                                "instructions": "If a secondary ordering key is requested, which direction applies to it?",
                                "criteria": directions},
    }
    call = client.call(state, questions)
    selected = call.answers["target"].get("choice")
    direction = call.answers["direction"].get("choice")
    explicit_direction = float(call.answers.get("direction_explicit", {}).get("noul", 1.0) or 0)
    secondary_selected = call.answers["secondary_target"].get("choice")
    secondary_direction = call.answers["secondary_direction"].get("choice")
    if selected not in target_choices or direction not in directions:
        raise RuntimeError("Jev selected an ordering outside supplied choices")
    if selected == "none":
        trace = ({"purpose": "resolve ordering", "state": state,
                  "choices": {"target": target_choices, "direction": directions},
                  "selected": None,
                  "probabilities": {key: call.answers.get(key, {}).get("probabilities")
                                    for key in questions},
                  "confidence": {key: call.answers.get(key, {}).get("confidence")
                                  for key in questions},
                  "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)
        return Ordering(None, None, "ambiguous", "jev_no_grounded_order", 1,
                        call.input_tokens, call.output_tokens, trace)
    if explicit_direction < .6:
        direction = "ASC"
    if secondary_selected not in secondary_choices or secondary_direction not in directions:
        raise RuntimeError("Jev selected a secondary ordering outside supplied choices")
    secondary_column = None
    if secondary_selected != "none" and secondary_selected != selected:
        secondary_column = choices[secondary_selected]["column"]
    mechanical_secondary = _explicit_secondary_order_clause(
        request, columns, choices[selected]["column"])
    if mechanical_secondary is not None:
        secondary_column, secondary_direction = mechanical_secondary
    trace = ({"purpose": "resolve ordering", "state": state,
              "choices": {"target": target_choices, "direction": directions,
                          "secondary_target": secondary_choices,
                          "secondary_direction": directions},
              "selected": {"column": choices[selected]["column"], "direction": direction,
                           "secondary_column": secondary_column,
                           "secondary_direction": secondary_direction if secondary_column else None},
              "probabilities": {key: call.answers.get(key, {}).get("probabilities") for key in questions},
              "confidence": {key: call.answers.get(key, {}).get("confidence") for key in questions},
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return Ordering(choices[selected]["column"], direction, "resolved", "jev_choice", 1,
                    call.input_tokens, call.output_tokens, trace,
                    secondary_column=secondary_column,
                    secondary_direction=secondary_direction if secondary_column else None)
