"""Conservative mechanical routing for explicit request operations.

Only unmistakable command/question forms are handled here. Ambiguous wording
falls back to System One. This reduces provider calls without turning natural
language semantics into a phrase dictionary.
"""

from __future__ import annotations

import re


_POLITE = re.compile(r"^\s*(?:(?:please|kindly)\s+)", re.IGNORECASE)


def explicit_request_operation(request: str) -> tuple[str, str | None] | None:
    """Return (mode, mutation operation) for an explicit surface form.

    ``mode`` is ``read`` or ``change``. Mutation operation is one of INSERT,
    UPDATE, DELETE. The matcher is intentionally conservative; anything less
    explicit is left to Jev.
    """
    if not isinstance(request, str) or not request.strip():
        return None
    text = _POLITE.sub("", request, count=1).lstrip()
    if re.match(r"delete\b", text, re.IGNORECASE):
        return "change", "DELETE"
    if re.match(r"update\b", text, re.IGNORECASE):
        return "change", "UPDATE"
    if re.match(r"insert\b", text, re.IGNORECASE):
        return "change", "INSERT"

    if re.match(r"(?:show|list|find|display|return|select|count|get)\b", text,
                re.IGNORECASE):
        return "read", None
    if re.match(r"(?:how\s+many|what\b|which\b|who\b|when\b|where\b|give\s+me\b)",
                text, re.IGNORECASE):
        return "read", None
    return None
