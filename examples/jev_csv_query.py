#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# =====================================================================
# System One / Jev
# =====================================================================

SYSTEM_ONE_URL = os.environ.get(
    "SYSTEM_ONE_URL",
    "https://api.typesafe.ai/v1/systemone",
)
SYSTEM_ONE_MODEL = os.environ.get(
    "SYSTEM_ONE_MODEL",
    "jev-latest",
)
SYSTEM_ONE_API_KEY = os.environ.get("SYSTEM_ONE_API_KEY")

# Environment-overridable pricing. Defaults match the pricing used in
# the earlier experiments: $0.042 / 1M input tokens, output free.
JEV_INPUT_USD_PER_MTOK = float(
    os.environ.get("JEV_INPUT_USD_PER_MTOK", "0.042")
)
JEV_OUTPUT_USD_PER_MTOK = float(
    os.environ.get("JEV_OUTPUT_USD_PER_MTOK", "0")
)

# =====================================================================
# Generic engine limits. These are computation controls, not domain rules.
# =====================================================================

MAX_PLANNER_STEPS = 14
MAX_FILTERS = 4
MAX_FILTER_CHOICES = 100
MAX_GROUP_CHOICES = 80
MAX_METRIC_CHOICES = 100
MAX_SORT_CHOICES = 20
MAX_RESULT_PREVIEW_ROWS = 15
MAX_DISTINCT_PROFILE_VALUES = 12
MAX_FILTER_DISTINCT_VALUES = 24

LIMIT_OPTIONS = [1, 3, 5, 10, 20]

# =====================================================================
# Stats
# =====================================================================

stats = {
    "jev_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "planner_calls": 0,
    "choice_calls": 0,
    "sql_executions": 0,
}

# =====================================================================
# Data model
# =====================================================================

@dataclass
class ColumnProfile:
    name: str
    sql_type: str
    logical_type: str
    null_count: int
    distinct_count: int
    examples: list[str] = field(default_factory=list)
    min_value: Any = None
    max_value: Any = None


@dataclass
class MetricChoice:
    key: str
    label: str
    sql: str
    alias: str
    description: str


@dataclass
class GroupChoice:
    key: str
    label: str
    sql: str | None
    alias: str | None
    description: str


@dataclass
class FilterChoice:
    key: str
    label: str
    sql: str | None
    params: tuple[Any, ...]
    description: str


@dataclass
class SortChoice:
    key: str
    label: str
    sql: str | None
    description: str


@dataclass
class QueryState:
    metric: MetricChoice | None = None
    group: GroupChoice | None = None
    filters: list[FilterChoice] = field(default_factory=list)
    sort: SortChoice | None = None
    limit: int | None = None

    executed: bool = False
    result_columns: list[str] = field(default_factory=list)
    result_rows: list[tuple[Any, ...]] = field(default_factory=list)
    result_total_rows: int = 0
    last_sql: str = ""
    last_params: tuple[Any, ...] = ()


@dataclass
class TraceEntry:
    stage: str
    selected: str
    probability: float
    confidence: float
    ranking: list[tuple[str, float]]

# =====================================================================
# Generic helpers
# =====================================================================

def short(text: Any, limit: int = 260) -> str:
    value = " ".join(str(text).split())
    if len(value) > limit:
        return value[:limit] + "..."
    return value


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def safe_alias(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", text.strip())
    value = value.strip("_") or "value"
    if value[0].isdigit():
        value = "v_" + value
    return value[:60]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def is_blank(value: str | None) -> bool:
    return value is None or not str(value).strip()


def parse_number(text: str) -> int | float | None:
    value = text.strip().replace(",", "")
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except Exception:
            return None
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        try:
            return float(value)
        except Exception:
            return None
    return None


DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
]


def parse_date(text: str) -> datetime | None:
    value = text.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            pass
    return None


def stringify_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        if math.isfinite(value):
            return f"{value:.6g}"
    return str(value)

# =====================================================================
# Jev API
# =====================================================================

def add_usage(response: dict[str, Any]) -> None:
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        return
    stats["input_tokens"] += int(
        usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    )
    stats["output_tokens"] += int(
        usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    )


def extract_answer(response: dict[str, Any], question_id: str) -> dict[str, Any]:
    answers = response.get("answers")
    if not isinstance(answers, dict):
        nested = response.get("result")
        if isinstance(nested, dict):
            answers = nested.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError(
            "System One response contained no answers object:\n"
            + json.dumps(response, indent=2)[:3000]
        )
    answer = answers.get(question_id)
    if not isinstance(answer, dict):
        raise RuntimeError(f"No answer for {question_id}")
    return answer


def jev_choice(
    *,
    state: dict[str, Any],
    instruction: str,
    candidates: list[tuple[Any, str]],
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("jev_choice received no candidates")

    if len(candidates) == 1:
        value, description = candidates[0]
        return {
            "selected": value,
            "confidence": 1.0,
            "ranking": [(value, description, 1.0)],
        }

    criteria = {
        f"c{i}": description
        for i, (_, description) in enumerate(candidates)
    }

    payload = {
        "model": SYSTEM_ONE_MODEL,
        "state": state,
        "questions": {
            "selection": {
                "type": "choice",
                "instructions": instruction,
                "criteria": criteria,
            }
        },
    }

    request = Request(
        SYSTEM_ONE_URL,
        data=json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SYSTEM_ONE_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "jev-csv-query/0.1",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print("\nSYSTEM ONE ERROR")
        print(body)
        raise
    except URLError as exc:
        raise RuntimeError(f"Could not reach System One: {exc}") from exc

    stats["jev_calls"] += 1
    stats["choice_calls"] += 1
    add_usage(result)

    answer = extract_answer(result, "selection")

    ranking: list[tuple[Any, str, float]] = []
    for key, probability in answer.get("probabilities", {}).items():
        try:
            index = int(key[1:])
        except Exception:
            continue
        if not 0 <= index < len(candidates):
            continue
        value, description = candidates[index]
        ranking.append((value, description, float(probability)))

    ranking.sort(key=lambda item: item[2], reverse=True)

    selected_key = answer.get("choice")
    if not selected_key:
        raise RuntimeError("System One returned no selected choice")

    selected_index = int(selected_key[1:])
    try:
        confidence = float(answer.get("confidence", 0.0))
    except Exception:
        confidence = 0.0

    return {
        "selected": candidates[selected_index][0],
        "confidence": confidence,
        "ranking": ranking,
    }

# =====================================================================
# CSV -> SQLite
# =====================================================================

def infer_column_type(values: list[str]) -> tuple[str, str]:
    nonblank = [v.strip() for v in values if not is_blank(v)]
    if not nonblank:
        return "TEXT", "text"

    numbers = [parse_number(v) for v in nonblank]
    if all(v is not None for v in numbers):
        if all(isinstance(v, int) for v in numbers):
            return "INTEGER", "integer"
        return "REAL", "real"

    dates = [parse_date(v) for v in nonblank]
    if all(v is not None for v in dates):
        return "TEXT", "date"

    return "TEXT", "text"


def convert_value(value: str, logical_type: str) -> Any:
    if is_blank(value):
        return None
    if logical_type == "integer":
        return int(str(value).replace(",", ""))
    if logical_type == "real":
        return float(str(value).replace(",", ""))
    if logical_type == "date":
        dt = parse_date(str(value))
        if dt is None:
            return str(value)
        # SQLite-friendly ISO representation.
        if dt.hour or dt.minute or dt.second or dt.microsecond:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d")
    return str(value)


def load_csv_to_sqlite(csv_path: Path) -> tuple[sqlite3.Connection, list[ColumnProfile]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise RuntimeError("CSV has no header row")
        headers = [h.strip() for h in reader.fieldnames]
        raw_rows = []
        for row in reader:
            raw_rows.append({h: (row.get(h) if row.get(h) is not None else "") for h in headers})

    if not raw_rows:
        raise RuntimeError("CSV contains no data rows")

    types: dict[str, tuple[str, str]] = {}
    for header in headers:
        values = [row[header] for row in raw_rows]
        types[header] = infer_column_type(values)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    column_defs = ", ".join(
        f"{qident(header)} {types[header][0]}"
        for header in headers
    )
    conn.execute(f"CREATE TABLE data ({column_defs})")

    placeholders = ",".join("?" for _ in headers)
    insert_sql = (
        "INSERT INTO data ("
        + ",".join(qident(h) for h in headers)
        + f") VALUES ({placeholders})"
    )

    converted_rows = []
    for row in raw_rows:
        converted_rows.append(
            tuple(
                convert_value(row[h], types[h][1])
                for h in headers
            )
        )

    conn.executemany(insert_sql, converted_rows)
    conn.commit()

    profiles: list[ColumnProfile] = []
    row_count = len(raw_rows)

    for header in headers:
        sql_type, logical_type = types[header]
        ident = qident(header)

        null_count = conn.execute(
            f"SELECT COUNT(*) FROM data WHERE {ident} IS NULL"
        ).fetchone()[0]
        distinct_count = conn.execute(
            f"SELECT COUNT(DISTINCT {ident}) FROM data WHERE {ident} IS NOT NULL"
        ).fetchone()[0]

        example_rows = conn.execute(
            f"SELECT {ident}, COUNT(*) AS n FROM data "
            f"WHERE {ident} IS NOT NULL "
            f"GROUP BY {ident} ORDER BY n DESC, {ident} LIMIT ?",
            (MAX_DISTINCT_PROFILE_VALUES,),
        ).fetchall()
        examples = [stringify_value(r[0]) for r in example_rows]

        min_value = None
        max_value = None
        if logical_type in {"integer", "real", "date"}:
            min_value, max_value = conn.execute(
                f"SELECT MIN({ident}), MAX({ident}) FROM data"
            ).fetchone()

        profiles.append(
            ColumnProfile(
                name=header,
                sql_type=sql_type,
                logical_type=logical_type,
                null_count=null_count,
                distinct_count=distinct_count,
                examples=examples,
                min_value=min_value,
                max_value=max_value,
            )
        )

    # Attach row_count as connection-side metadata through a temp table.
    conn.execute("CREATE TEMP TABLE __meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO __meta VALUES ('row_count', ?)", (str(row_count),))
    return conn, profiles


def get_row_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM __meta WHERE key='row_count'").fetchone()
    return int(row[0])

# =====================================================================
# Schema descriptions
# =====================================================================

def profile_description(profile: ColumnProfile) -> str:
    parts = [
        f"column '{profile.name}'",
        f"type={profile.logical_type}",
        f"distinct={profile.distinct_count}",
        f"nulls={profile.null_count}",
    ]
    if profile.examples:
        parts.append("examples=[" + ", ".join(profile.examples[:8]) + "]")
    if profile.min_value is not None or profile.max_value is not None:
        parts.append(
            f"range={stringify_value(profile.min_value)}..{stringify_value(profile.max_value)}"
        )
    return short("; ".join(parts), 360)


def schema_state(profiles: list[ColumnProfile], row_count: int) -> list[str]:
    return [
        f"rows={row_count}",
        *[profile_description(p) for p in profiles],
    ]

# =====================================================================
# Automatically generated query choices
# =====================================================================

def generate_metric_choices(profiles: list[ColumnProfile]) -> list[MetricChoice]:
    choices: list[MetricChoice] = [
        MetricChoice(
            key="count_rows",
            label="COUNT rows",
            sql="COUNT(*)",
            alias="row_count",
            description="Count rows in the selected population.",
        )
    ]

    for p in profiles:
        ident = qident(p.name)
        alias_base = safe_alias(p.name)

        # Counting non-null or unique values is valid for every column.
        choices.append(
            MetricChoice(
                key=f"count_nonnull:{p.name}",
                label=f"COUNT non-null {p.name}",
                sql=f"COUNT({ident})",
                alias=f"count_{alias_base}",
                description=(
                    f"Count non-null values of {profile_description(p)}"
                ),
            )
        )
        choices.append(
            MetricChoice(
                key=f"count_distinct:{p.name}",
                label=f"COUNT DISTINCT {p.name}",
                sql=f"COUNT(DISTINCT {ident})",
                alias=f"distinct_{alias_base}",
                description=(
                    f"Count unique values of {profile_description(p)}"
                ),
            )
        )

        if p.logical_type in {"integer", "real"}:
            for func, human in [
                ("SUM", "total"),
                ("AVG", "average"),
                ("MIN", "minimum"),
                ("MAX", "maximum"),
            ]:
                choices.append(
                    MetricChoice(
                        key=f"{func.lower()}:{p.name}",
                        label=f"{func}({p.name})",
                        sql=f"{func}({ident})",
                        alias=f"{func.lower()}_{alias_base}",
                        description=(
                            f"Compute the {human} of numeric {profile_description(p)}"
                        ),
                    )
                )

    return choices[:MAX_METRIC_CHOICES]


def generate_group_choices(profiles: list[ColumnProfile]) -> list[GroupChoice]:
    choices: list[GroupChoice] = [
        GroupChoice(
            key="none",
            label="NO GROUPING",
            sql=None,
            alias=None,
            description="Return one aggregate value for the whole filtered population.",
        )
    ]

    for p in profiles:
        ident = qident(p.name)
        alias_base = safe_alias(p.name)
        choices.append(
            GroupChoice(
                key=f"raw:{p.name}",
                label=p.name,
                sql=ident,
                alias=alias_base,
                description=f"Group rows by {profile_description(p)}",
            )
        )

        if p.logical_type == "date":
            choices.extend(
                [
                    GroupChoice(
                        key=f"year:{p.name}",
                        label=f"{p.name} by year",
                        sql=f"strftime('%Y', {ident})",
                        alias=f"{alias_base}_year",
                        description=(
                            f"Group date column '{p.name}' into calendar years."
                        ),
                    ),
                    GroupChoice(
                        key=f"month:{p.name}",
                        label=f"{p.name} by month",
                        sql=f"strftime('%Y-%m', {ident})",
                        alias=f"{alias_base}_month",
                        description=(
                            f"Group date column '{p.name}' into calendar year-month buckets."
                        ),
                    ),
                    GroupChoice(
                        key=f"day:{p.name}",
                        label=f"{p.name} by day",
                        sql=f"date({ident})",
                        alias=f"{alias_base}_day",
                        description=(
                            f"Group date column '{p.name}' into calendar days."
                        ),
                    ),
                ]
            )

    return choices[:MAX_GROUP_CHOICES]


def question_numbers(question: str) -> list[int | float]:
    values: list[int | float] = []
    for token in re.findall(r"[-+]?(?:\d+\.\d+|\d+)", question.replace(",", "")):
        parsed = parse_number(token)
        if parsed is not None and parsed not in values:
            values.append(parsed)
    return values


MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def generate_filter_choices(
    conn: sqlite3.Connection,
    profiles: list[ColumnProfile],
    question: str,
    existing: list[FilterChoice],
) -> list[FilterChoice]:
    existing_keys = {f.key for f in existing}
    choices: list[FilterChoice] = [
        FilterChoice(
            key="stop_filters",
            label="NO MORE FILTERS",
            sql=None,
            params=(),
            description="Do not add another input-row filter.",
        )
    ]

    question_norm = normalize_text(question)
    numeric_literals = question_numbers(question)
    month_terms = {
        month_num
        for word, month_num in MONTH_NAMES.items()
        if re.search(rf"\b{re.escape(word)}\b", question_norm)
    }

    # Categorical / low-cardinality value filters generated from actual data.
    for p in profiles:
        ident = qident(p.name)

        if p.distinct_count <= MAX_FILTER_DISTINCT_VALUES:
            rows = conn.execute(
                f"SELECT {ident}, COUNT(*) AS n FROM data "
                f"WHERE {ident} IS NOT NULL "
                f"GROUP BY {ident} ORDER BY n DESC, {ident} LIMIT ?",
                (MAX_FILTER_DISTINCT_VALUES,),
            ).fetchall()
            for row in rows:
                value = row[0]
                label_value = stringify_value(value)
                key = f"eq:{p.name}:{label_value}"
                if key in existing_keys:
                    continue
                choices.append(
                    FilterChoice(
                        key=key,
                        label=f"{p.name} = {label_value}",
                        sql=f"{ident} = ?",
                        params=(value,),
                        description=(
                            f"Keep rows where column '{p.name}' equals actual dataset value "
                            f"{label_value!r}. {profile_description(p)}"
                        ),
                    )
                )

        # Numeric thresholds come from numbers literally present in the question.
        if p.logical_type in {"integer", "real"}:
            for number in numeric_literals:
                for op, opname in [
                    (">", "greater than"),
                    (">=", "at least"),
                    ("<", "less than"),
                    ("<=", "at most"),
                    ("=", "equal to"),
                ]:
                    key = f"num:{p.name}:{op}:{number}"
                    if key in existing_keys:
                        continue
                    choices.append(
                        FilterChoice(
                            key=key,
                            label=f"{p.name} {op} {number}",
                            sql=f"{ident} {op} ?",
                            params=(number,),
                            description=(
                                f"Keep rows where numeric column '{p.name}' is {opname} "
                                f"the question-supplied number {number}. {profile_description(p)}"
                            ),
                        )
                    )

        # Date filters are generated from the actual data and calendar terms.
        if p.logical_type == "date":
            # Month-of-year filters if the question names a month.
            for month_num in sorted(month_terms):
                month_text = f"{month_num:02d}"
                key = f"monthnum:{p.name}:{month_text}"
                if key not in existing_keys:
                    choices.append(
                        FilterChoice(
                            key=key,
                            label=f"month({p.name}) = {month_text}",
                            sql=f"strftime('%m', {ident}) = ?",
                            params=(month_text,),
                            description=(
                                f"Keep rows whose date column '{p.name}' falls in calendar "
                                f"month {month_text}."
                            ),
                        )
                    )

            month_rows = conn.execute(
                f"SELECT DISTINCT strftime('%Y-%m', {ident}) AS ym "
                f"FROM data WHERE {ident} IS NOT NULL "
                f"ORDER BY ym"
            ).fetchall()
            months = [r[0] for r in month_rows if r[0]]
            if len(months) <= 36:
                for ym in months:
                    key = f"ym:{p.name}:{ym}"
                    if key in existing_keys:
                        continue
                    choices.append(
                        FilterChoice(
                            key=key,
                            label=f"{p.name} month = {ym}",
                            sql=f"strftime('%Y-%m', {ident}) = ?",
                            params=(ym,),
                            description=(
                                f"Keep rows from actual calendar month {ym} in date column '{p.name}'."
                            ),
                        )
                    )

    # Keep dataset-value candidates first. For larger datasets this is just a
    # payload cap; Jev still makes the semantic decision among retained choices.
    return choices[:MAX_FILTER_CHOICES]


def generate_sort_choices(state: QueryState) -> list[SortChoice]:
    if not state.result_columns:
        return []

    choices = [
        SortChoice(
            key="none",
            label="NO SORT",
            sql=None,
            description="Leave result rows in their current order.",
        )
    ]

    for column in state.result_columns:
        ident = qident(column)
        choices.extend(
            [
                SortChoice(
                    key=f"asc:{column}",
                    label=f"{column} ascending",
                    sql=f"{ident} ASC",
                    description=f"Sort result rows by '{column}' from smallest/earliest/A-Z upward.",
                ),
                SortChoice(
                    key=f"desc:{column}",
                    label=f"{column} descending",
                    sql=f"{ident} DESC",
                    description=f"Sort result rows by '{column}' from largest/latest/Z-A downward.",
                ),
            ]
        )

    return choices[:MAX_SORT_CHOICES]

# =====================================================================
# SQL compilation / execution
# =====================================================================

def where_clause(filters: list[FilterChoice]) -> tuple[str, tuple[Any, ...]]:
    if not filters:
        return "", ()
    parts = []
    params: list[Any] = []
    for f in filters:
        if f.sql is None:
            continue
        parts.append(f"({f.sql})")
        params.extend(f.params)
    if not parts:
        return "", ()
    return " WHERE " + " AND ".join(parts), tuple(params)


def compile_query(state: QueryState) -> tuple[str, tuple[Any, ...]]:
    if state.metric is None:
        raise RuntimeError("Cannot execute without a metric")

    select_parts = []
    group_sql = None

    if state.group and state.group.sql:
        group_alias = state.group.alias or "group_value"
        select_parts.append(f"{state.group.sql} AS {qident(group_alias)}")
        group_sql = state.group.sql

    select_parts.append(
        f"{state.metric.sql} AS {qident(state.metric.alias)}"
    )

    where_sql, params = where_clause(state.filters)

    sql = "SELECT " + ", ".join(select_parts) + " FROM data" + where_sql

    if group_sql:
        sql += f" GROUP BY {group_sql}"

    if state.sort and state.sort.sql:
        sql += f" ORDER BY {state.sort.sql}"

    if state.limit is not None:
        sql += " LIMIT ?"
        params = tuple(params) + (int(state.limit),)

    return sql, params


def execute_query(conn: sqlite3.Connection, state: QueryState) -> None:
    sql, params = compile_query(state)
    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()
    columns = [item[0] for item in cursor.description]

    state.executed = True
    state.result_columns = columns
    state.result_rows = [tuple(row) for row in rows[:MAX_RESULT_PREVIEW_ROWS]]
    state.result_total_rows = len(rows)
    state.last_sql = sql
    state.last_params = params
    stats["sql_executions"] += 1


def invalidate_result(state: QueryState) -> None:
    state.executed = False
    state.result_columns = []
    state.result_rows = []
    state.result_total_rows = 0
    state.last_sql = ""
    state.last_params = ()

# =====================================================================
# State descriptions
# =====================================================================

def current_plan_text(state: QueryState) -> str:
    parts = []
    parts.append("metric=" + (state.metric.label if state.metric else "UNSET"))
    parts.append("group=" + (state.group.label if state.group else "UNSET"))
    if state.filters:
        parts.append("filters=[" + "; ".join(f.label for f in state.filters) + "]")
    else:
        parts.append("filters=[]")
    parts.append("sort=" + (state.sort.label if state.sort else "UNSET"))
    parts.append("limit=" + (str(state.limit) if state.limit is not None else "ALL"))
    parts.append("executed=" + str(state.executed).lower())
    return " | ".join(parts)


def result_preview_text(state: QueryState) -> str:
    if not state.executed:
        return "No query result yet."
    if not state.result_rows:
        return (
            f"Executed query returned 0 rows. columns={state.result_columns}."
        )

    lines = [" | ".join(state.result_columns)]
    for row in state.result_rows:
        lines.append(" | ".join(stringify_value(v) for v in row))
    suffix = ""
    if state.result_total_rows > len(state.result_rows):
        suffix = f"\n... {state.result_total_rows - len(state.result_rows)} additional rows not shown"
    return "\n".join(lines) + suffix

# =====================================================================
# Choice trace helpers
# =====================================================================

def probability_for_selected(result: dict[str, Any]) -> float:
    selected = result["selected"]
    for value, _, probability in result["ranking"]:
        if value is selected or value == selected:
            return probability
    return 0.0


def add_trace(
    trace: list[TraceEntry],
    stage: str,
    result: dict[str, Any],
    label_fn,
) -> None:
    selected_label = label_fn(result["selected"])
    ranking = [
        (label_fn(value), probability)
        for value, _, probability in result["ranking"][:8]
    ]
    trace.append(
        TraceEntry(
            stage=stage,
            selected=selected_label,
            probability=probability_for_selected(result),
            confidence=result["confidence"],
            ranking=ranking,
        )
    )

# =====================================================================
# Component selection
# =====================================================================

def choose_metric(
    question: str,
    profiles: list[ColumnProfile],
    row_count: int,
    trace: list[TraceEntry],
) -> MetricChoice:
    choices = generate_metric_choices(profiles)
    result = jev_choice(
        state={
            "user_question": question,
            "dataset_schema": schema_state(profiles, row_count),
        },
        instruction=(
            "Choose the computed quantity that the user's question ultimately needs. "
            "Every option is an executable aggregate generated from the dataset schema. "
            "Match the semantic meaning of the question to the column and aggregation. "
            "Examples: questions about total revenue usually need SUM of a monetary column; "
            "questions asking how many rows/events need COUNT rows; questions asking how many "
            "unique entities need COUNT DISTINCT of the entity column; questions about an average "
            "need AVG of the relevant numeric column. Do not choose based only on lexical overlap."
        ),
        candidates=[(c, f"{c.label}. {c.description}") for c in choices],
    )
    add_trace(trace, "METRIC", result, lambda x: x.label)
    return result["selected"]


def choose_group(
    question: str,
    profiles: list[ColumnProfile],
    row_count: int,
    state: QueryState,
    trace: list[TraceEntry],
) -> GroupChoice:
    choices = generate_group_choices(profiles)
    result = jev_choice(
        state={
            "user_question": question,
            "dataset_schema": schema_state(profiles, row_count),
            "current_query_plan": current_plan_text(state),
        },
        instruction=(
            "Choose how the metric should be partitioned into comparable groups. "
            "All choices come from actual dataset columns or mechanically derived date buckets. "
            "Choose NO GROUPING when the question asks for one aggregate over the whole filtered "
            "population. Choose a column when the answer must compare categories/entities. "
            "For time questions, prefer the date bucket matching the requested granularity."
        ),
        candidates=[(c, f"{c.label}. {c.description}") for c in choices],
    )
    add_trace(trace, "GROUP", result, lambda x: x.label)
    return result["selected"]


def choose_filter(
    conn: sqlite3.Connection,
    question: str,
    profiles: list[ColumnProfile],
    row_count: int,
    state: QueryState,
    trace: list[TraceEntry],
) -> FilterChoice:
    choices = generate_filter_choices(
        conn,
        profiles,
        question,
        state.filters,
    )
    result = jev_choice(
        state={
            "user_question": question,
            "dataset_schema": schema_state(profiles, row_count),
            "current_query_plan": current_plan_text(state),
            "current_result": result_preview_text(state),
        },
        instruction=(
            "Choose the next input-row restriction required by the user's question. "
            "Filter values are generated from actual values in the dataset or numeric/date literals "
            "from the question. Select a filter only when the question explicitly or implicitly "
            "restricts the population to that value/threshold/time period. If the current filters "
            "already describe the intended population, choose NO MORE FILTERS."
        ),
        candidates=[(c, f"{c.label}. {c.description}") for c in choices],
    )
    add_trace(trace, "FILTER", result, lambda x: x.label)
    return result["selected"]


def choose_sort(
    question: str,
    state: QueryState,
    trace: list[TraceEntry],
) -> SortChoice:
    choices = generate_sort_choices(state)
    if not choices:
        raise RuntimeError("No sort choices available; execute the query first")
    result = jev_choice(
        state={
            "user_question": question,
            "current_query_plan": current_plan_text(state),
            "executed_result": result_preview_text(state),
        },
        instruction=(
            "Choose the ordering that makes the executed result answer the user's question. "
            "For highest/most/largest/latest questions, descending order on the relevant result "
            "column is usually appropriate. For lowest/least/smallest/earliest, ascending is usually "
            "appropriate. Choose NO SORT when row ordering is irrelevant."
        ),
        candidates=[(c, f"{c.label}. {c.description}") for c in choices],
    )
    add_trace(trace, "SORT", result, lambda x: x.label)
    return result["selected"]


def choose_limit(
    question: str,
    state: QueryState,
    trace: list[TraceEntry],
) -> int | None:
    candidates: list[tuple[int | None, str]] = [
        (None, "ALL ROWS. Return every result row after sorting."),
    ]
    for n in LIMIT_OPTIONS:
        candidates.append(
            (n, f"TOP {n}. Keep only the first {n} result row(s) after sorting.")
        )

    result = jev_choice(
        state={
            "user_question": question,
            "current_query_plan": current_plan_text(state),
            "executed_result": result_preview_text(state),
        },
        instruction=(
            "Choose how many result rows the user's question asks to keep. "
            "A singular 'which X has the most' usually needs TOP 1. 'Top five' needs TOP 5. "
            "Questions asking for all groups should keep ALL ROWS."
        ),
        candidates=candidates,
    )
    add_trace(
        trace,
        "LIMIT",
        result,
        lambda x: "ALL ROWS" if x is None else f"TOP {x}",
    )
    return result["selected"]

# =====================================================================
# Self-directed planner
# =====================================================================

ANSWER_NOW = "ANSWER_NOW"
CHOOSE_GROUP = "CHOOSE_GROUP"
ADD_FILTER = "ADD_FILTER"
EXECUTE = "EXECUTE"
SET_SORT = "SET_SORT"
SET_LIMIT = "SET_LIMIT"
CHANGE_METRIC = "CHANGE_METRIC"
CHANGE_GROUP = "CHANGE_GROUP"
CLEAR_SORT_LIMIT = "CLEAR_SORT_LIMIT"
STOP_NO_ANSWER = "STOP_NO_ANSWER"


def planner_candidates(state: QueryState) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []

    if state.group is None:
        candidates.append(
            (
                CHOOSE_GROUP,
                "Choose whether/how to group the metric into categories, entities, or time buckets.",
            )
        )
    elif state.executed:
        candidates.append(
            (
                CHANGE_GROUP,
                "Change the grouping because the current result partitions the data incorrectly.",
            )
        )

    if len(state.filters) < MAX_FILTERS:
        candidates.append(
            (
                ADD_FILTER,
                "Add another row filter generated from actual dataset values or question literals.",
            )
        )

    if not state.executed:
        candidates.append(
            (
                EXECUTE,
                "Execute the current valid query plan now and inspect the real result.",
            )
        )
    else:
        candidates.extend(
            [
                (
                    SET_SORT,
                    "Choose or change how the actual result rows are ordered.",
                ),
                (
                    SET_LIMIT,
                    "Choose or change how many result rows are kept.",
                ),
                (
                    CHANGE_METRIC,
                    "Change the computed metric because the current result measures the wrong thing.",
                ),
                (
                    CLEAR_SORT_LIMIT,
                    "Remove sorting and row limit before trying a different interpretation.",
                ),
                (
                    ANSWER_NOW,
                    "Use the current executed result as the final answer. The answer will come directly from SQL, not generated prose.",
                ),
                (
                    STOP_NO_ANSWER,
                    "Stop because the available columns/operations cannot answer the question reliably.",
                ),
            ]
        )

    return candidates


def choose_planner_action(
    question: str,
    profiles: list[ColumnProfile],
    row_count: int,
    state: QueryState,
    step: int,
    trace: list[TraceEntry],
) -> str:
    candidates = planner_candidates(state)
    result = jev_choice(
        state={
            "user_question": question,
            "dataset_schema": schema_state(profiles, row_count),
            "planner_step": step,
            "current_query_plan": current_plan_text(state),
            "current_executed_result": result_preview_text(state),
        },
        instruction=(
            "Control construction of an executable analytical query. "
            "Each action creates the next valid choice space from the dataset or executes the current program. "
            "Choose the action that most directly reduces what is still missing between the user's question "
            "and the current query/result. If the current SQL result already answers the question exactly, "
            "choose ANSWER_NOW. Do not keep searching merely to make the program longer. If the result is wrong, "
            "change the specific component that is wrong."
        ),
        candidates=candidates,
    )
    stats["planner_calls"] += 1
    add_trace(trace, "PLANNER", result, lambda x: x)
    return result["selected"]

# =====================================================================
# Main compilation loop
# =====================================================================

def run_compiler(
    conn: sqlite3.Connection,
    profiles: list[ColumnProfile],
    question: str,
) -> tuple[QueryState, list[TraceEntry], str]:
    row_count = get_row_count(conn)
    trace: list[TraceEntry] = []
    state = QueryState()

    # One mandatory first semantic choice: what quantity are we computing?
    state.metric = choose_metric(
        question,
        profiles,
        row_count,
        trace,
    )

    stop_reason = "step budget exhausted"

    for step in range(1, MAX_PLANNER_STEPS + 1):
        action = choose_planner_action(
            question,
            profiles,
            row_count,
            state,
            step,
            trace,
        )

        if action == CHOOSE_GROUP or action == CHANGE_GROUP:
            state.group = choose_group(
                question,
                profiles,
                row_count,
                state,
                trace,
            )
            invalidate_result(state)
            state.sort = None
            state.limit = None
            continue

        if action == ADD_FILTER:
            chosen = choose_filter(
                conn,
                question,
                profiles,
                row_count,
                state,
                trace,
            )
            if chosen.sql is not None:
                state.filters.append(chosen)
                invalidate_result(state)
                state.sort = None
                state.limit = None
            continue

        if action == EXECUTE:
            # If grouping has never been explicitly decided, no grouping is a
            # valid implicit default. The planner can still change it after seeing the result.
            if state.group is None:
                state.group = GroupChoice(
                    key="none",
                    label="NO GROUPING",
                    sql=None,
                    alias=None,
                    description="Implicit no-grouping default before first execution.",
                )
            execute_query(conn, state)
            continue

        if action == SET_SORT:
            if not state.executed:
                execute_query(conn, state)
            state.sort = choose_sort(question, state, trace)
            execute_query(conn, state)
            continue

        if action == SET_LIMIT:
            if not state.executed:
                execute_query(conn, state)
            state.limit = choose_limit(question, state, trace)
            execute_query(conn, state)
            continue

        if action == CHANGE_METRIC:
            state.metric = choose_metric(
                question,
                profiles,
                row_count,
                trace,
            )
            invalidate_result(state)
            state.sort = None
            state.limit = None
            continue

        if action == CLEAR_SORT_LIMIT:
            state.sort = None
            state.limit = None
            invalidate_result(state)
            continue

        if action == ANSWER_NOW:
            if not state.executed:
                execute_query(conn, state)
            stop_reason = "planner selected ANSWER_NOW"
            return state, trace, stop_reason

        if action == STOP_NO_ANSWER:
            stop_reason = "planner selected STOP_NO_ANSWER"
            return state, trace, stop_reason

    return state, trace, stop_reason

# =====================================================================
# Output
# =====================================================================

def cost_usd() -> float:
    return (
        stats["input_tokens"] / 1_000_000 * JEV_INPUT_USD_PER_MTOK
        + stats["output_tokens"] / 1_000_000 * JEV_OUTPUT_USD_PER_MTOK
    )


def print_schema(profiles: list[ColumnProfile], row_count: int) -> None:
    print("\nDATASET")
    print("=" * 78)
    print(f"Rows: {row_count}")
    for p in profiles:
        print("- " + profile_description(p))


def print_trace(trace: list[TraceEntry]) -> None:
    print("\nSELF-GENERATED CHOICE TRACE")
    print("=" * 78)
    for i, item in enumerate(trace, start=1):
        print(
            f"{i:02d}. {item.stage}: {item.selected} "
            f"P={item.probability:.1%} confidence={item.confidence:.1%}"
        )
        for label, probability in item.ranking[:5]:
            print(f"      {probability:6.1%}  {label}")


def print_plan(state: QueryState, stop_reason: str) -> None:
    print("\nFINAL QUERY PROGRAM")
    print("=" * 78)
    print(current_plan_text(state))
    print(f"Stop reason: {stop_reason}")
    if state.last_sql:
        print("\nSQL:")
        print(state.last_sql)
        if state.last_params:
            print("params:", state.last_params)


def print_answer(question: str, state: QueryState) -> None:
    print("\nFINAL ANSWER")
    print("=" * 78)
    print("Q:", question)

    if not state.executed:
        print("A: [no executable answer was produced]")
        return

    if not state.result_rows:
        print("A: [query returned no rows]")
        return

    print("A:")
    widths = [len(c) for c in state.result_columns]
    for row in state.result_rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(stringify_value(value)))
    widths = [min(w, 40) for w in widths]

    def fmt_row(values):
        cells = []
        for i, value in enumerate(values):
            text = stringify_value(value)
            if len(text) > widths[i]:
                text = text[: widths[i] - 3] + "..."
            cells.append(text.ljust(widths[i]))
        return " | ".join(cells)

    print("   " + fmt_row(state.result_columns))
    print("   " + "-+-".join("-" * w for w in widths))
    for row in state.result_rows:
        print("   " + fmt_row(row))
    if state.result_total_rows > len(state.result_rows):
        print(
            f"   ... {state.result_total_rows - len(state.result_rows)} additional result rows"
        )


def print_stats() -> None:
    print("\nSTATS / USAGE")
    print("=" * 78)
    print("System One calls:", stats["jev_calls"])
    print("Planner calls:", stats["planner_calls"])
    print("SQL executions:", stats["sql_executions"])
    print("Input tokens:", f"{stats['input_tokens']:,}")
    print("Output tokens:", f"{stats['output_tokens']:,}")
    print("Estimated run cost:", f"${cost_usd():.8f}")


def print_bottom_cost() -> None:
    total = cost_usd()
    average = total / stats["jev_calls"] if stats["jev_calls"] else 0.0
    print("\nRUN COST")
    print("=" * 78)
    print(f"System One calls: {stats['jev_calls']}")
    print(f"Input tokens: {stats['input_tokens']:,}")
    print(f"Output tokens: {stats['output_tokens']:,}")
    print(f"Input price: ${JEV_INPUT_USD_PER_MTOK:.3f} / 1M tokens")
    print(f"Output price: ${JEV_OUTPUT_USD_PER_MTOK:.3f} / 1M tokens")
    print(f"Estimated cost: ${total:.8f}")
    print(f"Estimated cost in cents: {total * 100:.5f}")
    print(f"Average cost per Jev call: ${average:.8f}")

# =====================================================================
# CLI
# =====================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Jev self-building CSV query compiler. The dataset generates columns, values, "
            "groupings, and executable operations; Jev chooses how to assemble them into a query."
        )
    )
    parser.add_argument("csv", help="CSV file to query")
    parser.add_argument("question", nargs="+", help="Natural-language analytical question")
    parser.add_argument(
        "--show-schema-only",
        action="store_true",
        help="Load/profile the CSV without calling Jev",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    conn, profiles = load_csv_to_sqlite(csv_path)
    row_count = get_row_count(conn)

    if args.show_schema_only:
        print_schema(profiles, row_count)
        return

    if not SYSTEM_ONE_API_KEY:
        raise SystemExit(
            "SYSTEM_ONE_API_KEY is not set.\n\n"
            "Example:\n"
            "  export SYSTEM_ONE_API_KEY='your-key'"
        )

    question = " ".join(args.question)

    print_schema(profiles, row_count)
    print("\nQUESTION")
    print("=" * 78)
    print(question)

    try:
        state, trace, stop_reason = run_compiler(
            conn,
            profiles,
            question,
        )

        print_stats()
        print_trace(trace)
        print_plan(state, stop_reason)
        print_answer(question, state)
        print_bottom_cost()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
