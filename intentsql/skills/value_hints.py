"""Small lexical samples from one chosen column; retrieval assigns no meaning."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from difflib import SequenceMatcher

from intentsql.skills.role_evidence import role_words
from intentsql.skills.schema import quote_identifier
from intentsql.skills.schema import Column


_WORDS = re.compile(r"[^\W\d_][\w']*", re.UNICODE)


def mechanically_related_values(request: str,
                                values: tuple[str, ...],
                                *,
                                column_name: str | None = None) -> tuple[tuple[str, dict], ...]:
    """Retrieve stored values with request-derived lexical evidence.

    This assigns no database meaning. It exposes exact phrases, compact
    prefixes, initialisms, and close spellings for a bounded semantic choice.
    """
    request_words = [match.group().casefold() for match in _WORDS.finditer(request)]
    phrases = {
        " ".join(request_words[start:start + width])
        for width in range(1, 5)
        for start in range(len(request_words) - width + 1)
    }
    initialisms: dict[str, list[str]] = {}
    for width in range(2, 5):
        for start in range(len(request_words) - width + 1):
            phrase = request_words[start:start + width]
            initialisms.setdefault("".join(word[0] for word in phrase), []).append(
                " ".join(phrase))
    # A word that continues the column name is role evidence, not a stored code.
    role = set(role_words(request, column_name)) if column_name else set()
    related: list[tuple[str, dict]] = []
    for value in values:
        stored_text = str(value)
        text = stored_text.strip()
        folded = text.casefold()
        code = re.sub(r"[^0-9A-Za-z]+", "", text).casefold()
        evidence: dict | None = None
        if folded in phrases:
            evidence = {"kind": "exact_request_phrase", "request_text": folded}
        elif 2 <= len(code) <= 4 and code in initialisms:
            evidence = {"kind": "request_initialism", "initialism": code,
                        "request_phrases": tuple(initialisms[code])}
        elif 2 <= len(code) <= 4:
            prefixes = sorted(word for word in request_words
                              if word not in role and len(word) > len(code)
                              and word.startswith(code))
            if prefixes:
                evidence = {"kind": "compact_observed_code_prefix",
                            "request_words": prefixes,
                            "normalized_observed_code": code}
        elif len(code) == 1 and len(values) <= 4:
            # Tiny one-character enum domains (for example L/R or Y/N) are
            # common database encodings.  A first-letter match is only
            # retrieval evidence: Jev must still decide semantic equivalence
            # against the competing observed values or NONE.  A word that
            # continues the column name is not evidence for that letter.
            prefixes = sorted(word for word in request_words
                              if word not in role and len(word) >= 4
                              and word.startswith(code))
            if prefixes:
                evidence = {"kind": "compact_observed_single_code_prefix",
                            "request_words": prefixes,
                            "normalized_observed_code": code}
        if evidence is None and 4 <= len(folded) <= 48:
            candidates = ((SequenceMatcher(None, folded, phrase).ratio(), phrase)
                          for phrase in phrases
                          if abs(len(folded) - len(phrase)) <= max(2, len(folded) // 4))
            score, phrase = max(candidates, default=(0.0, ""))
            if score >= .84:
                evidence = {"kind": "near_spelling", "request_text": phrase,
                            "similarity": round(score, 2)}
        if evidence is not None:
            # Match against normalized text, but preserve the exact stored
            # operand so parameterized equality still matches padded/canonical
            # database values.
            related.append((stored_text, evidence))
    return tuple(related)


def small_category_hints(connection: sqlite3.Connection, table: str,
                         columns: tuple[Column, ...], max_values: int = 6) -> dict[str, tuple[str, ...]]:
    """Show only complete, tiny categorical domains from the selected table."""
    hints = {}
    row_count = connection.execute(
        f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
    for column in columns:
        declared = column.declared_type.upper()
        if column.primary_key or not any(kind in declared for kind in ("TEXT", "CHAR", "CLOB")):
            continue
        rows = connection.execute(
            f"SELECT DISTINCT {quote_identifier(column.name)} FROM {quote_identifier(table)} "
            f"WHERE {quote_identifier(column.name)} IS NOT NULL LIMIT ?",
            (max_values + 1,)).fetchall()
        values = tuple(str(row[0]) for row in rows)
        if (values and len(values) <= max_values and len(values) < row_count and
                all(len(value) <= 64 for value in values)):
            hints[column.name] = values
    return hints


def targeted_values(connection: sqlite3.Connection, table: str, column: str,
                    request: str, limit: int = 8,
                    include_extrema: bool = False, *, include_common: bool = False) -> tuple[str, ...]:
    """Retrieve candidate values after a column is chosen, never a whole table.

    Token overlap is used only to find possible evidence. Jev decides whether
    any value and comparison operator express the user's intent.
    """
    if limit < 1:
        return ()
    tokens = sorted({word.lower() for word in _WORDS.findall(request) if len(word) >= 3},
                    key=lambda word: (-len(word), word))[:10]
    table_sql, column_sql = quote_identifier(table), quote_identifier(column)
    words = [match.group().casefold() for match in _WORDS.finditer(request)]
    surface_forms = list(dict.fromkeys(
        phrase for width in range(1, 5)
        for start in range(len(words) - width + 1)
        if (len(phrase := " ".join(words[start:start + width])) >= 3 or
            (width == 1 and len(phrase) == 1 and phrase not in {"a", "i"}))))[:200]
    exact: set[str] = set()
    if surface_forms:
        placeholders = ", ".join("?" for _ in surface_forms)
        rows = connection.execute(
            f"SELECT DISTINCT CAST({column_sql} AS TEXT) FROM {table_sql} "
            f"WHERE LOWER(TRIM(CAST({column_sql} AS TEXT))) IN ({placeholders}) LIMIT ?",
            (*surface_forms, limit)).fetchall()
        exact = {row[0] for row in rows}
    candidates: set[str] = set()
    for token in tokens:
        escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = connection.execute(
            f"SELECT DISTINCT {column_sql} FROM {table_sql} "
            f"WHERE {column_sql} IS NOT NULL AND CAST({column_sql} AS TEXT) "
            "LIKE ? ESCAPE '\\' LIMIT 25", (f"%{escaped}%",)).fetchall()
        candidates.update(str(row[0]) for row in rows)
    # Short stored codes often represent a natural-language category (for
    # example an abbreviated enum). Prefer codes that are a mechanical prefix
    # of a request token, but do not assign their meaning here. Jev still has
    # to choose the semantic equivalent from bounded observed values.
    code_rows = connection.execute(
        f"SELECT DISTINCT CAST({column_sql} AS TEXT) FROM {table_sql} "
        f"WHERE {column_sql} IS NOT NULL AND LENGTH(CAST({column_sql} AS TEXT)) BETWEEN 2 AND 4 "
        "LIMIT 128").fetchall()
    request_words = [match.group().casefold() for match in _WORDS.finditer(request)]
    request_initialisms = {
        "".join(word[0] for word in request_words[start:start + width])
        for width in range(2, 5)
        for start in range(len(request_words) - width + 1)
    }
    for row in code_rows:
        value = str(row[0])
        code = re.sub(r"[^0-9A-Za-z]+", "", value).casefold()
        if (2 <= len(code) <= 4 and
                (code in request_initialisms or
                 any(len(word) > len(code) and word.startswith(code)
                     for word in request_words))):
            candidates.add(value)
    # A small complete value domain can supply typo-tolerant *retrieval*.
    # This never assigns a value's role: Jev must still bind the observed
    # candidate, and larger domains are not arbitrarily truncated as evidence.
    fuzzy: list[str] = []
    if not exact:
        domain = connection.execute(
            f"SELECT DISTINCT CAST({column_sql} AS TEXT) FROM {table_sql} "
            f"WHERE {column_sql} IS NOT NULL LIMIT 129").fetchall()
        if len(domain) <= 128:
            ranked = []
            for (value,) in domain:
                label = str(value).casefold()
                if len(label) < 5 or len(label) > 48:
                    continue
                score = max((SequenceMatcher(None, label, phrase).ratio()
                             for phrase in surface_forms
                             if abs(len(label) - len(phrase)) <= max(2, len(label) // 4)),
                            default=0.0)
                if score >= .84:
                    ranked.append((score, str(value)))
            fuzzy = [value for _, value in sorted(ranked, key=lambda item: (-item[0], item[1]))[:3]]
    extrema: set[str] = set()
    if include_extrema:
        row = connection.execute(
            f"SELECT MIN({column_sql}), MAX({column_sql}) FROM {table_sql} "
            f"WHERE {column_sql} IS NOT NULL").fetchone()
        extrema = {str(value) for value in row if value is not None}
    if not candidates:
        rows = connection.execute(
            f"SELECT DISTINCT {column_sql} FROM {table_sql} "
            f"WHERE {column_sql} IS NOT NULL LIMIT ?", (limit,)).fetchall()
        candidates.update(str(row[0]) for row in rows)
    def score(value: str) -> tuple[int, int, int, str]:
        lowered = value.casefold()
        value_words = {match.group().casefold() for match in _WORDS.finditer(value)}
        exact_overlap = sum(len(word) for word in request_words if word in value_words)
        normalized_code = re.sub(r"[^0-9A-Za-z]+", "", value).casefold()
        prefix_overlap = 0
        if 3 <= len(normalized_code) <= 4:
            prefix_overlap = max((len(normalized_code) for word in request_words
                                  if len(word) > len(normalized_code) and
                                  word.startswith(normalized_code)), default=0)
        # Exact token evidence outranks a compact-code prefix. Arbitrary
        # substring coincidences such as "many" inside "Germany" contribute
        # nothing and therefore cannot masquerade as semantic grounding.
        return (-exact_overlap, -prefix_overlap, len(value), lowered)
    leading = (sorted(exact, key=lambda value: (-len(value), value)) + fuzzy)[:limit]
    remaining_extrema = sorted(extrema - set(leading), key=score)[:max(0, limit - len(leading))]
    remaining = sorted(candidates - set(leading) - set(remaining_extrema), key=score)[:max(0, limit - len(leading) - len(remaining_extrema))]
    result = leading + remaining_extrema + remaining
    if include_common:
        # Lexical overlap alone can miss semantic aliases for common stored
        # categories. Expose a tiny frequency sample, never assign its meaning.
        common = connection.execute(
            f"SELECT {column_sql} FROM {table_sql} WHERE typeof({column_sql}) = 'text' "
            f"GROUP BY {column_sql} ORDER BY COUNT(*) DESC, {column_sql} LIMIT 3").fetchall()
        result = list(dict.fromkeys(result[:max(0, limit - 3)] + [str(row[0]) for row in common] + result))[:limit]
    return tuple(result)


def profiled_text_patterns(connection: sqlite3.Connection, table: str, column: str,
                           limit: int = 6) -> tuple[str, ...]:
    """Return recurring parenthetical annotations mechanically found in stored text.

    These are operand candidates, not interpreted meanings. Jev must still bind
    a request to a candidate before code may compile a LIKE predicate.
    """
    table_sql, column_sql = quote_identifier(table), quote_identifier(column)
    rows = connection.execute(
        f"SELECT DISTINCT CAST({column_sql} AS TEXT) FROM {table_sql} "
        f"WHERE {column_sql} IS NOT NULL LIMIT 2048").fetchall()
    counts: Counter[str] = Counter()
    for (raw,) in rows:
        text = str(raw)
        for match in re.finditer(r"\([^()]{2,32}\)", text):
            counts[match.group()] += 1
    return tuple(value for value, count in sorted(
        counts.items(), key=lambda item: (-item[1], item[0].casefold()))
        if count >= 2)[:limit]
