"""Lexical evidence that a request word continues a schema column name.

This evidence nominates a role. It never adds a predicate or a stored value.
"""
from __future__ import annotations

import re


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in _TOKEN.findall(value.casefold().replace("_", " "))
                 if len(token) >= 3)


def explicitly_names_column(request: str, column_name: str) -> bool:
    label = re.escape(column_name).replace("_", r"[_\s]+")
    return bool(re.search(r"(?<!\w)" + label + r"(?!\w)", request, re.I))


def role_words(request: str, column_name: str) -> tuple[str, ...]:
    """Request words that share a stem with this column name.

    A shared stem is a common prefix of at least three characters that covers
    the shorter word except for one inflectional character. "batters" shares
    a stem with "bats"; "hitters" does not. The result is evidence for a
    bounded decision, not a column assignment.
    """
    column_tokens = _tokens(column_name)
    found: list[str] = []
    for word in _tokens(request):
        for token in column_tokens:
            length = 0
            for left, right in zip(word, token):
                if left != right:
                    break
                length += 1
            shorter = min(len(word), len(token))
            if length >= 3 and length >= shorter - 1 and length / shorter >= 0.6:
                found.append(word)
                break
    return tuple(dict.fromkeys(found))
