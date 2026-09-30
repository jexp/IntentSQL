"""Resolve the presentation order of a small, already-selected output set."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
import re
from typing import Any

from intentsql.jev_client import JevClient


@dataclass(frozen=True)
class OutputOrder:
    labels: tuple[str, ...]
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _surface_pattern(label: str) -> re.Pattern[str]:
    """Match a selected column label as natural request text.

    Qualified labels use only their leaf column because the table is already
    established. Underscores/spaces/hyphens are interchangeable presentation
    separators; no schema-specific aliases are introduced here.
    """
    leaf = label.rsplit(".", 1)[-1]
    parts = [re.escape(part) for part in re.split(r"[_\s-]+", leaf) if part]
    return re.compile(r"(?<!\w)" + r"[\s_-]+".join(parts) + r"(?!\w)", re.I)


def _mechanical_request_order(request: str, labels: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return explicit request order when it can be grounded mechanically.

    Exact schema-name surface matches are preferred.  For an explicit RETURN
    list, if every slot but one maps uniquely to an already-selected field, the
    remaining field may occupy the remaining slot by elimination.  This keeps
    presentation order stable even when one requested attribute uses a semantic
    paraphrase rather than the literal schema identifier.
    """
    projection_text = request
    explicit_return = re.search(r"\breturn\b", request, re.I)
    if explicit_return:
        projection_text = request[explicit_return.end():]
        projection_text = re.split(
            r"(?:[,;]\s*)?(?:ordered|sorted)\s+by\b",
            projection_text, maxsplit=1, flags=re.I)[0]
        slots = [slot.strip(" .") for slot in re.split(
            r"\s*,\s*|\s+and\s+", projection_text, flags=re.I) if slot.strip(" .")]
        if len(slots) == len(labels):
            mapped: dict[int, str] = {}
            used_labels: set[str] = set()
            for index, slot in enumerate(slots):
                matches = [label for label in labels
                           if _surface_pattern(label).search(slot)]
                if len(matches) == 1 and matches[0] not in used_labels:
                    mapped[index] = matches[0]
                    used_labels.add(matches[0])
            remaining_slots = [index for index in range(len(slots)) if index not in mapped]
            remaining_labels = [label for label in labels if label not in used_labels]
            if len(remaining_slots) == len(remaining_labels) == 1:
                mapped[remaining_slots[0]] = remaining_labels[0]
            if len(mapped) == len(labels):
                return tuple(mapped[index] for index in range(len(slots)))

    positioned: list[tuple[int, str]] = []
    for label in labels:
        match = _surface_pattern(label).search(projection_text)
        if match is None:
            return None
        positioned.append((match.start(), label))
    starts = [position for position, _ in positioned]
    if len(set(starts)) != len(starts):
        return None
    return tuple(label for _, label in sorted(positioned))


def resolve_output_order(request: str, labels: tuple[str, ...],
                         client: JevClient, *, entity_label: str | None = None) -> OutputOrder:
    """Preserve explicit field order mechanically; ask Jev only if needed."""
    if len(labels) < 2:
        return OutputOrder(labels)
    mechanical = _mechanical_request_order(request, labels)
    if mechanical is not None:
        return OutputOrder(mechanical, trace=({
            "purpose": "preserve explicitly named output-field order",
            "selected": mechanical,
        },))
    if len(labels) > 3:
        return OutputOrder(labels)
    orders = tuple(permutations(labels))
    criteria = {f"o{index}": list(order) for index, order in enumerate(orders)}
    state = {"request": request, "selected_output_fields": labels,
             "task": "Order only the already-selected returned fields; do not add or remove fields."}
    if entity_label in labels:
        state["established_entity_label"] = entity_label
        state["task"] += (" When the request asks which entity has a measure, place its human-readable "
                          "label before the measure unless the user specifies another field order.")
    call = client.call(state, {"order": {"type": "choice",
        "instructions": "Which candidate matches the requested output-field order?",
        "criteria": criteria}})
    answer = call.answers["order"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("System One selected an invalid output-field order")
    chosen = tuple(criteria[selected])
    trace = ({"purpose": "resolve requested output-field order", "state": state,
              "choices": criteria, "selected": chosen,
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return OutputOrder(chosen, 1, call.input_tokens, call.output_tokens, trace)
