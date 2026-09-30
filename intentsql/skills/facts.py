"""Literal facts extracted mechanically, without assigning semantic roles."""

from __future__ import annotations

import re
import calendar
from dataclasses import dataclass
from datetime import date


_QUOTED = re.compile(r"``(?P<treebank>.*?)''|(['\"])(?:\\.|(?!\2).)*?\2")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTHS = {name.casefold(): index for index in range(1, 13)
           for name in (calendar.month_name[index], calendar.month_abbr[index])}
_MONTH_PATTERN = "|".join(re.escape(name) for name in sorted(_MONTHS, key=len, reverse=True))
_NATURAL_DATE = re.compile(
    rf"(?<!\w)(?:(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?|"
    rf"(?P<day_first>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month_after>{_MONTH_PATTERN}))"
    rf",?\s+(?P<year>\d{{4}})(?!\w)", re.IGNORECASE)
_MONTH_YEAR = re.compile(
    rf"(?<!\w)(?P<month>{_MONTH_PATTERN})\s+(?P<year>\d{{4}})(?!\w)",
    re.IGNORECASE)
_MONTH_RANGE_YEAR = re.compile(
    rf"(?<!\w)(?P<lower>{_MONTH_PATTERN})\s+"
    rf"(?:through|to|until|-)\s+"
    rf"(?P<upper>{_MONTH_PATTERN})\s+(?P<year>\d{{4}})(?!\w)",
    re.IGNORECASE)
_INTEGER = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?!\w|\.\d)")
_DECIMAL = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*\.\d+(?!\w)")
_DIGIT_SCALED = re.compile(
    r"(?<![\w.])(?P<number>-?\d+(?:,\d{3})*(?:\.\d+)?)\s+"
    r"(?P<scale>hundred|thousand|million|billion)\b", re.IGNORECASE)
_COMPACT_SCALED = re.compile(
    r"(?<![\w.])(?P<number>-?\d+(?:,\d{3})*(?:\.\d+)?)(?P<scale>[kKmMbB])\b")
_COMPACT_SCALES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "dozen": 12,
}
_NUMBER_SCALES = {"hundred": 100, "thousand": 1_000,
                  "million": 1_000_000, "billion": 1_000_000_000}
_WORD_TOKEN = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class LiteralFact:
    kind: str  # integer, date, invalid_date, quoted_text
    value: int | str
    raw: str
    start: int
    end: int

    @property
    def fact_id(self) -> str:
        """Stable provenance key for role assignment and traces."""
        return f"{self.kind}:{self.start}:{self.end}"


def extract_facts(request: str) -> tuple[LiteralFact, ...]:
    if not isinstance(request, str):
        raise TypeError("request must be text")
    facts: list[LiteralFact] = []
    excluded: list[tuple[int, int]] = []
    # A shared trailing year is ordinary calendar syntax: preserve both exact
    # month operands before the single-month matcher consumes the upper one.
    for match in _MONTH_RANGE_YEAR.finditer(request):
        year = int(match.group("year"))
        lower = _MONTHS[match.group("lower").casefold()]
        upper = _MONTHS[match.group("upper").casefold()]
        facts.extend((
            LiteralFact("month_year", f"{year:04d}-{lower:02d}",
                        match.group("lower"), match.start("lower"), match.end("lower")),
            LiteralFact("month_year", f"{year:04d}-{upper:02d}",
                        request[match.start("upper"):match.end("year")],
                        match.start("upper"), match.end("year")),
        ))
        excluded.append((match.start(), match.end()))
    for pattern, kind in ((_QUOTED, "quoted_text"), (_DATE, "date"),
                          (_NATURAL_DATE, "natural_date"),
                          (_MONTH_YEAR, "month_year")):
        for match in pattern.finditer(request):
            if any(match.start() < end and match.end() > start for start, end in excluded):
                continue
            if kind == "natural_date":
                month = _MONTHS[(match.group("month") or match.group("month_after")).casefold()]
                day = int(match.group("day") or match.group("day_first"))
                try:
                    value = date(int(match.group("year")), month, day).isoformat()
                except ValueError:
                    value = match.group()
                    fact_kind = "invalid_date"
                else:
                    fact_kind = "date"
            elif kind == "month_year":
                month = _MONTHS[match.group("month").casefold()]
                value = f"{int(match.group('year')):04d}-{month:02d}"
                fact_kind = "month_year"
            else:
                value = ((match.group("treebank") if match.group("treebank") is not None
                          else match.group()[1:-1]).strip()
                         if kind == "quoted_text" else match.group())
                fact_kind = kind
                if kind == "date":
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        fact_kind = "invalid_date"
            facts.append(LiteralFact(fact_kind, value, match.group(), match.start(), match.end()))
            excluded.append((match.start(), match.end()))
    # Numeric syntax with an explicit scale is exact request evidence too.
    # Capture it before ordinary integers/decimals so ``12 million`` remains
    # one operand (12_000_000) rather than the unrelated literal ``12``.
    for match in _DIGIT_SCALED.finditer(request):
        if any(match.start() < end and match.end() > start for start, end in excluded):
            continue
        raw_number = match.group("number").replace(",", "")
        mantissa = float(raw_number) if "." in raw_number else int(raw_number)
        value = mantissa * _NUMBER_SCALES[match.group("scale").casefold()]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        facts.append(LiteralFact("worded_number", value, match.group(),
                                 match.start(), match.end()))
        excluded.append((match.start(), match.end()))
    # Compact magnitude notation is exact syntax too. Keep this deliberately
    # narrow (30k, 2.5m, 1B): whitespace-separated single letters are too
    # ambiguous with ordinary units such as metres.
    for match in _COMPACT_SCALED.finditer(request):
        if any(match.start() < end and match.end() > start for start, end in excluded):
            continue
        raw_number = match.group("number").replace(",", "")
        mantissa = float(raw_number) if "." in raw_number else int(raw_number)
        value = mantissa * _COMPACT_SCALES[match.group("scale").casefold()]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        facts.append(LiteralFact("worded_number", value, match.group(),
                                 match.start(), match.end()))
        excluded.append((match.start(), match.end()))
    for match in _DECIMAL.finditer(request):
        if any(match.start() < end and match.end() > start for start, end in excluded):
            continue
        facts.append(LiteralFact("number", float(match.group().replace(",", "")),
                                 match.group(), match.start(), match.end()))
        excluded.append((match.start(), match.end()))
    for match in _INTEGER.finditer(request):
        if any(match.start() < end and match.end() > start for start, end in excluded):
            continue
        raw = match.group()
        facts.append(LiteralFact("integer", int(raw.replace(",", "")), raw,
                                 match.start(), match.end()))
    # Worded quantities are syntax/data, not an interpretation of their role.
    # Keep the original span and exact composed value so a later Jev decision
    # can bind it to a range, filter, assignment, or limit without regenerating
    # the operand.
    words = list(_WORD_TOKEN.finditer(request))
    index = 0
    while index < len(words):
        start_index = index
        total = current = 0
        saw_number = False
        while index < len(words):
            token = words[index].group().casefold()
            if token == "and":
                index += 1
                continue
            if token in _NUMBER_WORDS:
                current += _NUMBER_WORDS[token]
                saw_number = True
            elif token in _NUMBER_SCALES and saw_number:
                scale = _NUMBER_SCALES[token]
                current = (current or 1) * scale
                if scale >= 1000:
                    total += current
                    current = 0
            else:
                break
            index += 1
        if saw_number:
            span_start, span_end = words[start_index].start(), words[index - 1].end()
            if not any(span_start < end and span_end > begin
                       for begin, end in excluded):
                raw = request[span_start:span_end]
                facts.append(LiteralFact("worded_number", total + current, raw,
                                         span_start, span_end))
                excluded.append((span_start, span_end))
        if index == start_index:
            index += 1
    return tuple(sorted(facts, key=lambda item: item.start))
