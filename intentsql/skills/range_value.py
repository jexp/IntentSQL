"""Resolve two inclusive operands from literal facts and column data format."""

from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import LiteralFact, extract_facts
from intentsql.skills.schema import quote_identifier


_ISO_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?\Z")


@dataclass(frozen=True)
class RangeBounds:
    lower: int | str | None
    upper: int | str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    fact_ids: tuple[str, ...] = ()


def explicit_inclusive_interval(request: str,
                               facts: tuple[LiteralFact, ...]) -> tuple[LiteralFact, LiteralFact] | None:
    """Return the two endpoints of one explicit inclusive interval, if present.

    `between A and B`, `from A to/through/until B`, and `A through/until B`
    are interval syntax. A separate threshold or limit elsewhere in the
    request does not turn those endpoints into a set of alternatives.
    Two different intervals are left unresolved.
    """
    exact = [fact for fact in facts if fact.kind in {"integer", "number"}]
    found: list[tuple[LiteralFact, LiteralFact]] = []
    for earlier, later in zip(sorted(exact, key=lambda fact: fact.start),
                              sorted(exact, key=lambda fact: fact.start)[1:]):
        prefix = request[max(0, earlier.start - 24):earlier.start]
        middle = request[earlier.end:later.start]
        between = (re.search(r"\bbetween\s*$", prefix, re.I) and
                   re.fullmatch(r"\s+and\s+", middle, re.I))
        ranged = (re.search(r"\bfrom\s*$", prefix, re.I) and
                  re.fullmatch(r"\s+(?:to|through|until)\s+", middle, re.I))
        through = bool(re.fullmatch(r"\s+(?:through|until)\s+", middle, re.I))
        if between or ranged or through:
            found.append((earlier, later))
    if len(found) != 1:
        return None
    return found[0]


def strict_date_bounds(request: str, facts: tuple[LiteralFact, ...]) -> RangeBounds:
    """Ground explicit after/before date comparisons without changing inclusivity."""
    dates = [fact for fact in facts if fact.kind == "date"]
    if len(dates) != 2:
        return RangeBounds(None, None, "none", "no_two_date_interval")
    lower, upper = dates
    before_lower = request[max(0, lower.start - 24):lower.start]
    between = request[lower.end:upper.start]
    if (re.search(r"\bafter\s*$", before_lower, re.I) and
            re.search(r"\bbefore\s*$", between, re.I)):
        return RangeBounds(lower.value, upper.value, "resolved",
                           "explicit_strict_date_comparisons",
                           fact_ids=(lower.fact_id, upper.fact_id))
    return RangeBounds(None, None, "none", "no_explicit_strict_comparisons")


def column_uses_iso_dates(connection: sqlite3.Connection, table: str, column: str) -> bool:
    """Inspect a small sample; no table or column name carries date semantics."""
    rows = connection.execute(
        f"SELECT {quote_identifier(column)} FROM {quote_identifier(table)} "
        f"WHERE {quote_identifier(column)} IS NOT NULL LIMIT 8").fetchall()
    return bool(rows) and all(_ISO_DATE.fullmatch(str(row[0])) for row in rows)


def resolve_range_bounds(request: str, column: str, date_formatted: bool,
                         client: JevClient | None = None,
                         facts: tuple[LiteralFact, ...] | None = None) -> RangeBounds:
    """Compose bounds from exact user literals; never invent literal values."""
    candidates = [fact for fact in (facts if facts is not None else extract_facts(request))
                  if fact.kind in ("integer", "number", "date", "month_year",
                                   "worded_number")]
    if date_formatted:
        # ISO date syntax gives a mechanical role to four-digit year facts.
        # Other numeric facts may describe limits or a different predicate.
        date_facts = [fact for fact in candidates if fact.kind in ("date", "month_year") or
                      (fact.kind == "integer" and 1000 <= fact.value <= 9999)]
        if date_facts:
            candidates = date_facts
        if len(candidates) == 1 and candidates[0].kind == "month_year":
            year, month = (int(part) for part in str(candidates[0].value).split("-", 1))
            last_day = calendar.monthrange(year, month)[1]
            return RangeBounds(f"{year:04d}-{month:02d}-01",
                               f"{year:04d}-{month:02d}-{last_day:02d}",
                               "resolved", "single_month_on_iso_date_column",
                               fact_ids=(candidates[0].fact_id,))
        if len(candidates) == 1 and candidates[0].kind == "integer":
            year = candidates[0].value
            return RangeBounds(f"{year:04d}-01-01", f"{year:04d}-12-31",
                               "resolved", "single_year_on_iso_date_column",
                               fact_ids=(candidates[0].fact_id,))
    if len(candidates) < 2:
        return RangeBounds(None, None, "ambiguous", "insufficient_bounds")
    calls = input_tokens = output_tokens = 0
    trace: tuple[dict[str, Any], ...] = ()
    if len(candidates) == 2:
        selected = candidates
        source = "two_literal_facts"
    else:
        client = client or JevClient()
        choices = {f"v{index}": {"value": fact.value, "offset": fact.start}
                   for index, fact in enumerate(candidates)}
        state = {"request": request, "range_column": column}
        questions = {
            "lower": {"type": "choice", "instructions": "Which extracted literal starts the inclusive column interval? Ignore output row count.", "criteria": choices},
            "upper": {"type": "choice", "instructions": "Which extracted literal ends the inclusive column interval? Ignore output row count.", "criteria": choices},
        }
        call = client.call(state, questions)
        ids = (call.answers["lower"].get("choice"), call.answers["upper"].get("choice"))
        if any(item not in choices for item in ids) or ids[0] == ids[1]:
            return RangeBounds(None, None, "ambiguous", "jev_bounds_unclear", 1,
                               call.input_tokens, call.output_tokens)
        selected = [candidates[int(item[1:])] for item in ids]
        calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
        source = "jev_literal_roles"
        trace = ({"purpose": "assign inclusive interval bound roles", "state": state,
                  "choices": choices, "selected": ids,
                  "probabilities": {key: call.answers[key].get("probabilities") for key in questions},
                  "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
                  "elapsed_ms": call.elapsed_ms},)

    values: list[int | str] = [fact.value for fact in selected]
    if date_formatted:
        # A year literal denotes its full calendar year only when the inspected
        # column actually stores ISO dates. The protocol already chose BETWEEN.
        def lower_date(value: int | str) -> str:
            if isinstance(value, int):
                return f"{value:04d}-01-01"
            if re.fullmatch(r"\d{4}-\d{2}", value):
                return f"{value}-01"
            return value

        def upper_date(value: int | str) -> str:
            if isinstance(value, int):
                return f"{value:04d}-12-31"
            if re.fullmatch(r"\d{4}-\d{2}", value):
                year, month = (int(part) for part in value.split("-"))
                return f"{value}-{calendar.monthrange(year, month)[1]:02d}"
            return value

        values = [lower_date(value) for value in values]
        upper_values = [upper_date(fact.value) for fact in selected]
        lower, upper = min(values), max(upper_values)
    else:
        if type(values[0]) is not type(values[1]):
            return RangeBounds(None, None, "ambiguous", "mixed_bound_types", calls,
                               input_tokens, output_tokens, trace)
        lower, upper = min(values), max(values)
    return RangeBounds(lower, upper, "resolved", source, calls,
                       input_tokens, output_tokens, trace,
                       tuple(fact.fact_id for fact in selected))
