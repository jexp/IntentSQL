"""Detect unsupported absence-of-related-row semantics before a direct join executes."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.schema import Column, Relation


_ABSENCE_CUE = re.compile(
    r"\b(?:never\s+(?:have|has|had)|(?:do|does|did)\s+not\s+(?:have|has)|"
    r"(?:have|has|had)\s+no|without)\b",
    re.IGNORECASE,
)


def should_probe_relation_absence(request: str) -> bool:
    """Return whether generic negative-existence wording deserves a bounded review.

    This only decides whether to ask the specialized skill.  It does not decide
    that a relation is absent and contains no schema/domain vocabulary.
    """
    return bool(_ABSENCE_CUE.search(request))


@dataclass(frozen=True)
class RelationAbsence:
    mode: str
    status: str
    source: str = "jev_relation_absence"
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def resolve_relation_absence(request: str, source_table: str,
                             related_table: str, relation: Relation,
                             source_columns: tuple[Column, ...],
                             related_columns: tuple[Column, ...],
                             client: JevClient) -> RelationAbsence:
    """Distinguish anti-join meaning from ordinary joined NULL predicates.

    The caller has already validated one direct foreign-key edge.  Jev chooses
    only among bounded interpretations of the negative wording; it cannot add
    a relation, column, or executable operation.
    """
    state = {
        "request": request,
        "source_table": source_table,
        "related_table": related_table,
        "validated_relation": {
            "child_table": relation.child_table,
            "child_column": relation.child_column,
            "parent_table": relation.parent_table,
            "parent_column": relation.parent_column,
        },
        "source_columns": [column.name for column in source_columns],
        "related_columns": [column.name for column in related_columns],
        "task": (
            "Classify only the meaning of the negative-existence wording.  An anti-join means "
            "the source row qualifies because no related row exists at all.  A joined NULL "
            "predicate means a related row exists and one of its stored fields is NULL."
        ),
    }
    criteria = {
        "anti_join_absence": (
            "Return source rows for which no row exists across the validated relationship "
            "(NOT EXISTS / anti-join semantics)."
        ),
        "joined_row_null": (
            "A related row must exist, but one of that related row's stored fields is missing/NULL."
        ),
        "ordinary_relation": (
            "The request uses the relation normally and does not require absence of the related row."
        ),
    }
    call = client.call(state, {"relation_absence": {
        "type": "choice",
        "instructions": "What does the negative-existence wording require?",
        "criteria": criteria,
    }})
    answer = call.answers["relation_absence"]
    mode = answer.get("choice")
    if mode not in criteria:
        raise RuntimeError("System One selected an invalid relation-absence mode")
    confidence = float(answer.get("confidence") or 0)
    status = "resolved" if confidence >= .65 else "ambiguous"
    trace = ({
        "purpose": "distinguish anti-join absence from a NULL field on an existing related row",
        "state": state,
        "choices": criteria,
        "selected": mode,
        "probabilities": answer.get("probabilities"),
        "confidence": confidence,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    return RelationAbsence(mode, status, jev_calls=1,
                           input_tokens=call.input_tokens,
                           output_tokens=call.output_tokens, trace=trace)
