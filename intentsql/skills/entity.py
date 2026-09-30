"""Choose one source table from actual inspected table names."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class Entity:
    table: str | None
    status: str  # resolved or ambiguous
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def explicitly_named_source(request: str, tables: tuple[str, ...] | list[str]) -> str | None:
    """Find a unique row-grain table named in the request, without model inference."""
    def stem(word: str) -> str:
        word = word.casefold()
        if len(word) > 4 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word

    words = [stem(word) for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", request)]
    counts = {}
    for table in tables:
        parts = [stem(word) for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", table)]
        counts[table] = sum(words[start:start + len(parts)] == parts
                            for start in range(len(words) - len(parts) + 1))
    if not counts:
        return None
    maximum = max(counts.values())
    winners = [table for table, count in counts.items() if count == maximum]
    return winners[0] if maximum and len(winners) == 1 else None


def resolve_entity(request: str, tables: tuple[str, ...] | list[str],
                   client: JevClient | None = None,
                   table_metadata: dict[str, Any] | None = None,
                   *, relationship_mode: bool = False,
                   relationship_context: dict[str, Any] | None = None) -> Entity:
    names = tuple(dict.fromkeys(tables))
    if not names:
        return Entity(None, "ambiguous", "no_tables")
    if len(names) == 1:
        return Entity(names[0], "resolved", "only_table")
    client = client or JevClient()
    criteria = {f"t{index}": name for index, name in enumerate(names)}
    if relationship_mode:
        criteria["unsupported"] = "No table can anchor this request through one inspected direct relationship"
        instructions = (
            "Which table should anchor the requested records? The request may use fields or one condition "
            "from one directly related table. Choose the table representing the primary row grain or a "
            "table from which one inspected direct foreign-key relationship can supply the remaining "
            "requested fields. Do not require every requested field to exist on the anchor table."
        )
    else:
        criteria["unspecified"] = "No single source table is identified by the request"
        instructions = "Which table contains the requested source records?"
    state = {"request": request, "candidate_tables": names}
    if table_metadata:
        state["candidate_table_columns"] = table_metadata
    if relationship_mode and relationship_context:
        state["candidate_direct_relationships"] = relationship_context
    questions = {"entity": {"type": "choice",
                            "instructions": instructions,
                            "criteria": criteria}}
    call = client.call(state, questions)
    answer = call.answers["entity"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("Jev selected a table outside the inspected schema")
    trace = ({"purpose": ("resolve direct-relationship anchor table" if relationship_mode
                           else "resolve source table"),
              "state": state,
              "choices": criteria, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    if selected in {"unspecified", "unsupported"}:
        return Entity(None, "ambiguous", "jev_unspecified", 1,
                      call.input_tokens, call.output_tokens, trace)
    return Entity(criteria[selected], "resolved", "jev_choice", 1,
                  call.input_tokens, call.output_tokens, trace)
