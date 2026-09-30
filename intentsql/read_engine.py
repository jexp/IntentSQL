#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from collections import Counter

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# =============================================================================
# CONFIG
# =============================================================================

SYSTEM_ONE_URL = os.environ.get(
    "SYSTEM_ONE_URL",
    "https://api.typesafe.ai/v1/systemone",
)

SYSTEM_ONE_MODEL = os.environ.get(
    "SYSTEM_ONE_MODEL",
    "jev-latest",
)

SYSTEM_ONE_API_KEY = os.environ.get(
    "SYSTEM_ONE_API_KEY",
)

JEV_INPUT_USD_PER_MTOK = float(
    os.environ.get(
        "JEV_INPUT_USD_PER_MTOK",
        "0.042",
    )
)

JEV_OUTPUT_USD_PER_MTOK = float(
    os.environ.get(
        "JEV_OUTPUT_USD_PER_MTOK",
        "0",
    )
)


# How many semantic compiler decisions may happen before giving up.
MAX_PLANNER_STEPS = int(
    os.environ.get(
        "JEV_DB_MAX_STEPS",
        "24",
    )
)

# How many times a candidate SQL query may fail semantic vetting
# before we stop.
MAX_REVISIONS = int(
    os.environ.get(
        "JEV_DB_MAX_REVISIONS",
        "3",
    )
)

# Maximum number of distinct values from ONE selected column
# exposed as Jev choices.
MAX_DISTINCT_VALUES = int(
    os.environ.get(
        "JEV_DB_MAX_VALUES",
        "120",
    )
)

MAX_RESULT_ROWS = int(
    os.environ.get(
        "JEV_DB_MAX_RESULT_ROWS",
        "5000",
    )
)
MAX_OUTPUTS = 8

# Independent semantic checks must all clear this threshold,
# in addition to the vet verdict choosing PASS.
VET_THRESHOLD = float(
    os.environ.get(
        "JEV_DB_VET_THRESHOLD",
        "0.68",
    )
)


# =============================================================================
# RUN STATS
# =============================================================================

stats = {
    "jev_calls": 0,
    "failed_calls": 0,
    "failed_calls_without_usage": 0,
    "input_tokens": 0,
    "output_tokens": 0,

    "planner_calls": 0,
    "vet_calls": 0,

    "sql_executions": 0,

    "schema_queries": 0,
    "value_queries": 0,
}


TRACE: list[dict[str, Any]] = []
EVENT_SINK = None
DECISION_EVIDENCE: list[dict[str, Any]] = []
RUN_DATABASE_NAME = ""
RUN_VALUE_HINTS: dict[str, dict[str, list[Any]]] = {}
RUN_ORDINAL_VALUES: dict[str, list[int]] = {}


def emit(kind: str, **payload: Any) -> None:
    if EVENT_SINK is not None:
        EVENT_SINK({"kind": kind, **payload})


def reset_run_state() -> None:

    for key in stats:
        stats[key] = 0

    TRACE.clear()
    DECISION_EVIDENCE.clear()
    RUN_VALUE_HINTS.clear()
    RUN_ORDINAL_VALUES.clear()


# =============================================================================
# HELPERS
# =============================================================================

def qident(
    name: str,
) -> str:

    return (
        '"'
        + name.replace(
            '"',
            '""',
        )
        + '"'
    )


def short(
    value: Any,
    limit: int = 300,
) -> str:

    text = re.sub(
        r"\s+",
        " ",
        str(value).strip(),
    )

    if len(text) <= limit:
        return text

    return (
        text[:limit]
        + "..."
    )


def stable_json(
    value: Any,
) -> str:

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def total_cost_usd() -> float:

    input_cost = (
        stats["input_tokens"]
        / 1_000_000
        * JEV_INPUT_USD_PER_MTOK
    )

    output_cost = (
        stats["output_tokens"]
        / 1_000_000
        * JEV_OUTPUT_USD_PER_MTOK
    )

    return (
        input_cost
        + output_cost
    )


def usage_stats() -> dict[str, Any]:
    return {
        "jev_calls": stats["jev_calls"],
        "input_tokens": stats["input_tokens"],
        "output_tokens": stats["output_tokens"],
        "failed_calls": stats["failed_calls"],
        "usage_complete": stats["failed_calls_without_usage"] == 0,
    }


def add_usage(
    response: dict[str, Any],
) -> None:

    usage = (
        response.get("usage")
        or {}
    )

    stats["input_tokens"] += int(
        usage.get(
            "input_tokens",
            0,
        )
        or 0
    )

    stats["output_tokens"] += int(
        usage.get(
            "output_tokens",
            0,
        )
        or 0
    )


def trace(
    kind: str,
    selected: str,
    probability: float,
    confidence: float,
    ranking: list[tuple[str, float]] | None = None,
) -> None:

    TRACE.append(
        {
            "kind":
                kind,

            "selected":
                selected,

            "probability":
                probability,

            "confidence":
                confidence,

            "ranking":
                ranking or [],
        }
    )


# =============================================================================
# SYSTEM ONE API
# =============================================================================

def system_one(
    state: Any,
    questions: dict[str, Any],
) -> dict[str, Any]:

    payload = {"state": state, "questions": questions}
    if SYSTEM_ONE_MODEL:
        payload["model"] = SYSTEM_ONE_MODEL

    headers = {**({"Authorization": f"Bearer {SYSTEM_ONE_API_KEY}"}
                  if SYSTEM_ONE_API_KEY else {}),
               "Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "IntentSQL/0.1.0-alpha.1"}
    retryable = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
    try:
        max_retries = max(0, int(os.environ.get("SYSTEM_ONE_RETRIES", "1")))
    except ValueError:
        max_retries = 1

    attempt = 0
    while True:
        attempt += 1
        started = time.monotonic()
        stats["jev_calls"] += 1
        call_number = stats["jev_calls"]
        emit("call_start", state=state, questions=questions, call=call_number,
             request_payload=payload, attempt=attempt)
        request = Request(
            SYSTEM_ONE_URL,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            code = exc.code
            retry_after = None
            try:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            except Exception:
                retry_after = None
            exc.close()
            try:
                error_result = json.loads(body)
            except Exception:
                error_result = None
            usage = error_result.get("usage") if isinstance(error_result, dict) else None
            if isinstance(usage, dict):
                add_usage(error_result)
            else:
                stats["failed_calls_without_usage"] += 1
            stats["failed_calls"] += 1
            message = f"System One HTTP {code}"
            emit("call_error", call=call_number, message=message,
                 usage=usage or {}, latency_ms=round((time.monotonic() - started) * 1000))
            if code in retryable and attempt <= max_retries:
                try:
                    delay = min(5.0, max(0.0, float(retry_after))) if retry_after else 0.5 * attempt
                except (TypeError, ValueError):
                    delay = 0.5 * attempt
                emit("call_retry", call=call_number, message=message,
                     attempt=attempt + 1, retry_in_ms=round(delay * 1000))
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"System One HTTP {code}. Check the provider settings and try again."
            ) from exc
        except URLError as exc:
            stats["failed_calls"] += 1
            stats["failed_calls_without_usage"] += 1
            emit("call_error", call=call_number, message=f"Could not reach System One: {exc}",
                 usage={}, latency_ms=round((time.monotonic() - started) * 1000))
            raise RuntimeError(f"Could not reach System One: {exc}") from exc

        add_usage(result)
        emit("call_end", answers=result.get("answers", {}), usage=result.get("usage", {}),
             response_payload=result, call=call_number,
             latency_ms=round((time.monotonic() - started) * 1000))
        return result

def choice(
    state: Any,
    instruction: Any,
    candidates: list[
        tuple[
            Any,
            Any,
        ]
    ],
    *,
    label: str,
) -> tuple[
    Any,
    dict[str, Any],
]:

    if not candidates:

        raise ValueError(
            f"No candidates "
            f"for {label}"
        )

    if len(candidates) == 1:

        value, description = (
            candidates[0]
        )

        result = {
            "choice":
                "c0",

            "confidence":
                1.0,

            "probabilities": {
                "c0":
                    1.0,
            },

            "type":
                "choice",
        }

        trace(
            label,
            str(
                description
            ),
            1.0,
            1.0,
            [
                (
                    str(
                        description
                    ),
                    1.0,
                )
            ],
        )
        evidence = {"stage": label, "selected": str(description), "probability": 1.0, "confidence": 1.0}
        DECISION_EVIDENCE.append(evidence)
        emit("decision", label=label, candidates=[{"label": description, "probability": 1.0}], selected=description, probability=1.0, confidence=1.0, automatic=True)

        return (
            value,
            result,
        )

    criteria = {
        f"c{i}":
            description

        for i, (
            _,
            description,
        ) in enumerate(
            candidates
        )
    }

    response = system_one(
        state,
        {
            "decision": {
                "type":
                    "choice",

                "instructions":
                    instruction,

                "criteria":
                    criteria,
            }
        },
    )

    answer = (
        response[
            "answers"
        ][
            "decision"
        ]
    )

    selected_index = int(
        answer[
            "choice"
        ][1:]
    )

    probabilities = (
        answer.get(
            "probabilities",
            {},
        )
    )

    ranking: list[
        tuple[
            str,
            float,
        ]
    ] = []

    for (
        key,
        probability,
    ) in probabilities.items():

        try:

            index = int(
                key[1:]
            )

        except Exception:

            continue

        if not (
            0
            <= index
            < len(
                candidates
            )
        ):
            continue

        ranking.append(
            (
                str(
                    candidates[
                        index
                    ][1]
                ),
                float(
                    probability
                ),
            )
        )

    ranking.sort(
        key=lambda item:
            item[1],

        reverse=True,
    )

    selected_probability = float(
        probabilities.get(
            answer[
                "choice"
            ],
            0.0,
        )
    )

    trace(
        label,

        str(
            candidates[
                selected_index
            ][1]
        ),

        selected_probability,

        float(
            answer.get(
                "confidence",
                0.0,
            )
        ),

        ranking[:6],
    )

    evidence = {
        "stage": label,
        "selected": str(candidates[selected_index][1]),
        "probability": selected_probability,
        "confidence": float(answer.get("confidence", 0.0)),
        "alternatives": ranking[1:3],
    }
    DECISION_EVIDENCE.append(evidence)
    emit("decision", label=label,
         candidates=[{"label": description, "probability": float(probabilities.get(f"c{i}", 0.0))}
                     for i, (_, description) in enumerate(candidates)],
         selected=candidates[selected_index][1], probability=selected_probability,
         confidence=evidence["confidence"], automatic=False)

    return (
        candidates[
            selected_index
        ][0],
        answer,
    )


# =============================================================================
# SCHEMA
# =============================================================================

@dataclass
class ColumnInfo:

    table: str
    name: str

    declared_type: str

    kind: str

    notnull: bool
    pk: bool

    @property
    def ref(
        self,
    ) -> str:

        return (
            f"{self.table}."
            f"{self.name}"
        )


@dataclass
class ForeignKey:

    from_table: str
    from_column: str

    to_table: str
    to_column: str

    def label(
        self,
    ) -> str:

        return (
            f"{self.from_table}."
            f"{self.from_column}"
            " -> "
            f"{self.to_table}."
            f"{self.to_column}"
        )


@dataclass
class TableInfo:

    name: str

    columns: list[
        ColumnInfo
    ]

    foreign_keys: list[
        ForeignKey
    ]


@dataclass
class SchemaInfo:

    tables: dict[
        str,
        TableInfo,
    ]

    foreign_keys: list[
        ForeignKey
    ]

    def compact_state(
        self,
    ) -> dict[str, Any]:

        return {
            "tables": {
                table.name: {
                    "columns": [
                        {
                            "name":
                                column.name,

                            "type":
                                (
                                    column.declared_type
                                    or column.kind
                                ),

                            "kind":
                                column.kind,

                            "primary_key":
                                column.pk,
                        }

                        for column
                        in table.columns
                    ],

                    "foreign_keys": [
                        fk.label()

                        for fk
                        in table.foreign_keys
                    ],
                }

                for table
                in self.tables.values()
            },

            "relationships": [
                fk.label()

                for fk
                in self.foreign_keys
            ],
        }


def infer_kind(
    declared_type: str,
    name: str,
) -> str:

    declared = (
        declared_type
        or ""
    ).upper()

    column_name = (
        name.lower()
    )

    # SQLite NUMERIC affinity is often used for ISO date strings. A
    # date/time column name is stronger evidence than that affinity.
    if (
        any(item in declared for item in ("DATE", "TIME"))
        or re.search(r"(^|_)(date|time|timestamp)($|_)", column_name)
    ):
        return "date"

    if any(
        item in declared
        for item in (
            "INT",
            "REAL",
            "FLOA",
            "DOUB",
            "NUM",
            "DEC",
        )
    ):
        return "number"

    if (
        "BOOL"
        in declared
    ):
        return "boolean"

    return "text"


def inspect_schema(
    conn: sqlite3.Connection,
) -> SchemaInfo:

    stats[
        "schema_queries"
    ] += 1

    table_rows = (
        conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        .fetchall()
    )

    tables: dict[
        str,
        TableInfo,
    ] = {}

    all_foreign_keys: list[
        ForeignKey
    ] = []

    for (
        table_name,
    ) in table_rows:

        stats[
            "schema_queries"
        ] += 1

        columns = []

        pragma = (
            f"PRAGMA "
            f"table_info("
            f"{qident(table_name)}"
            f")"
        )

        for row in (
            conn.execute(
                pragma
            )
            .fetchall()
        ):

            # cid, name, type, notnull,
            # dflt_value, pk

            columns.append(
                ColumnInfo(
                    table=
                        table_name,

                    name=
                        row[1],

                    declared_type=
                        row[2]
                        or "",

                    kind=
                        infer_kind(
                            row[2]
                            or "",
                            row[1],
                        ),

                    notnull=
                        bool(
                            row[3]
                        ),

                    pk=
                        bool(
                            row[5]
                        ),
                )
            )

        stats[
            "schema_queries"
        ] += 1

        foreign_keys = []

        pragma = (
            f"PRAGMA "
            f"foreign_key_list("
            f"{qident(table_name)}"
            f")"
        )

        for row in (
            conn.execute(
                pragma
            )
            .fetchall()
        ):

            # id, seq, table, from, to,
            # on_update, on_delete, match

            fk = ForeignKey(
                from_table=
                    table_name,

                from_column=
                    row[3],

                to_table=
                    row[2],

                to_column=
                    row[4],
            )

            foreign_keys.append(
                fk
            )

            all_foreign_keys.append(
                fk
            )

        tables[
            table_name
        ] = TableInfo(
            name=
                table_name,

            columns=
                columns,

            foreign_keys=
                foreign_keys,
        )

    if not tables:

        raise RuntimeError(
            "Database has no user tables"
        )

    return SchemaInfo(
        tables=
            tables,

        foreign_keys=
            all_foreign_keys,
    )


# =============================================================================
# QUERY PROGRAM
# =============================================================================

@dataclass
class JoinSpec:

    left_table: str
    left_column: str

    right_table: str
    right_column: str
    extra_on: tuple[tuple[str, str, str, str], ...] = ()

    def label(
        self,
    ) -> str:

        primary = (
            f"{self.left_table}."
            f"{self.left_column}"
            " = "
            f"{self.right_table}."
            f"{self.right_column}"
        )
        if not self.extra_on:
            return primary
        return primary + " AND " + " AND ".join(
            f"{left_table}.{left_column} = {right_table}.{right_column}"
            for left_table, left_column, right_table, right_column in self.extra_on
        )


@dataclass
class SelectExpr:

    # column | aggregate
    kind: str

    table: str | None = None
    column: str | None = None

    func: str | None = None

    # year | month | day
    transform: str | None = None

    alias: str | None = None

    def label(
        self,
    ) -> str:

        if (
            self.kind
            == "column"
        ):

            base = (
                f"{self.table}."
                f"{self.column}"
            )

            if self.transform:

                return (
                    f"{base} "
                    f"by "
                    f"{self.transform}"
                )

            return base

        if (
            self.func
            == "COUNT_ROWS"
        ):

            return (
                "COUNT rows"
            )

        if (
            self.func
            == "COUNT_DISTINCT"
        ):

            return (
                "COUNT DISTINCT "
                f"{self.table}."
                f"{self.column}"
            )

        return (
            f"{self.func}("
            f"{self.table}."
            f"{self.column}"
            f")"
        )


@dataclass
class FilterSpec:

    table: str
    column: str

    operator: str

    value: Any

    transform: str | None = None

    def label(
        self,
    ) -> str:

        left = (
            f"{self.table}."
            f"{self.column}"
        )

        if self.transform:

            left += (
                f" by "
                f"{self.transform}"
            )

        if self.operator in ("IS NULL", "IS NOT NULL"):
            return f"{left} {self.operator}"
        return f"{left} {self.operator} {self.value!r}"


@dataclass
class GroupSpec:

    table: str
    column: str

    transform: str | None = None

    def label(
        self,
    ) -> str:

        base = (
            f"{self.table}."
            f"{self.column}"
        )

        if self.transform:

            return (
                f"{base} "
                f"by "
                f"{self.transform}"
            )

        return base


@dataclass
class OrderSpec:

    # Output alias
    key: str

    direction: str


@dataclass
class HavingSpec:
    key: str
    operator: str
    value: Any

    def label(self) -> str:
        return f"{self.key} {self.operator} {self.value!r}"


@dataclass
class QueryProgram:

    base_table: str
    operation: str = field(default="SELECT", init=False)

    joins: list[
        JoinSpec
    ] = field(
        default_factory=list
    )

    outputs: list[
        SelectExpr
    ] = field(
        default_factory=list
    )

    filters: list[
        FilterSpec
    ] = field(
        default_factory=list
    )

    groups: list[
        GroupSpec
    ] = field(
        default_factory=list
    )

    having: list[HavingSpec] = field(default_factory=list)

    order_by: list[
        OrderSpec
    ] = field(
        default_factory=list
    )

    limit: int | None = None

    distinct: bool = False
    distinct_decided: bool = False

    # {
    #   "key": output alias,
    #   "mode": "max" | "min"
    # }
    extremum: dict[
        str,
        str,
    ] | None = None

    def tables(
        self,
    ) -> list[str]:

        result = [
            self.base_table
        ]

        for join in self.joins:

            if (
                join.left_table
                not in result
            ):

                result.append(
                    join.left_table
                )

            if (
                join.right_table
                not in result
            ):

                result.append(
                    join.right_table
                )

        return result

    def state(
        self,
    ) -> dict[str, Any]:

        return {
            "operation": self.operation,
            "base_table":
                self.base_table,

            "joined_tables":
                self.tables(),

            "joins": [
                join.label()

                for join
                in self.joins
            ],

            "outputs": [
                output.label()

                for output
                in self.outputs
            ],

            "filters": [
                filter_.label()

                for filter_
                in self.filters
            ],

            "groups": [
                group.label()

                for group
                in self.groups
            ],

            "having": [condition.label() for condition in self.having],

            "order_by": [
                asdict(
                    order
                )

                for order
                in self.order_by
            ],

            "limit":
                self.limit,

            "distinct":
                (
                    self.distinct

                    if (
                        self
                        .distinct_decided
                    )

                    else "UNSET"
                ),

            "extremum":
                self.extremum,
        }


# =============================================================================
# COLUMN / JOIN ENUMERATION
# =============================================================================

def table_columns(
    schema: SchemaInfo,
    program: QueryProgram,
) -> list[ColumnInfo]:

    result: list[
        ColumnInfo
    ] = []

    for table in (
        program.tables()
    ):

        result.extend(
            schema
            .tables[
                table
            ]
            .columns
        )

    return result


def joined_edge_keys(
    program: QueryProgram,
) -> set[
    tuple[
        str,
        str,
        str,
        str,
    ]
]:

    result = set()

    for join in program.joins:

        result.add(
            (
                join.left_table,
                join.left_column,
                join.right_table,
                join.right_column,
            )
        )

        result.add(
            (
                join.right_table,
                join.right_column,
                join.left_table,
                join.left_column,
            )
        )

    return result


def available_joins(
    schema: SchemaInfo,
    program: QueryProgram,
) -> list[JoinSpec]:

    present = set(
        program.tables()
    )

    used = (
        joined_edge_keys(
            program
        )
    )

    result: list[
        JoinSpec
    ] = []

    for fk in (
        schema.foreign_keys
    ):

        from_present = (
            fk.from_table
            in present
        )

        to_present = (
            fk.to_table
            in present
        )

        # We want exactly one side
        # already in the relation.
        if (
            from_present
            == to_present
        ):

            continue

        if from_present:

            join = JoinSpec(
                left_table=
                    fk.from_table,

                left_column=
                    fk.from_column,

                right_table=
                    fk.to_table,

                right_column=
                    fk.to_column,
            )

        else:

            join = JoinSpec(
                left_table=
                    fk.to_table,

                left_column=
                    fk.to_column,

                right_table=
                    fk.from_table,

                right_column=
                    fk.from_column,
            )

        key = (
            join.left_table,
            join.left_column,
            join.right_table,
            join.right_column,
        )

        if key not in used:

            result.append(
                join
            )

            # Tables may represent observations at more than one grain.
            # Generate legal same-named numeric/date equalities as optional
            # join refinements; Jev decides whether the request needs one.
            right_columns = {column.name: column for column in schema.tables[join.right_table].columns}
            for present_table in present:
                for left_column in schema.tables[present_table].columns:
                    right_column = right_columns.get(left_column.name)
                    if (right_column is None or left_column.pk or right_column.pk
                            or left_column.name == "id" or left_column.name.endswith("_id")
                            or left_column.kind not in ("number", "date")
                            or right_column.kind != left_column.kind):
                        continue
                    result.append(JoinSpec(
                        join.left_table, join.left_column,
                        join.right_table, join.right_column,
                        ((present_table, left_column.name,
                          join.right_table, right_column.name),),
                    ))

    return result


# =============================================================================
# STATE
# =============================================================================

def compiler_state(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram | None,
    stage: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:

    state: dict[
        str,
        Any,
    ] = {
        "task": (
            "Compile the user's database question "
            "into a read-only relational query by "
            "choosing only from executable options "
            "supplied by the program."
        ),

        "user_question":
            question,
        "database_name": RUN_DATABASE_NAME,

        "database_schema":
            schema.compact_state(),

        "stage":
            stage,

        "current_program":
            (
                program.state()

                if program
                else None
            ),
        "decision_evidence": DECISION_EVIDENCE[-10:],
        "observed_categories": {
            table: RUN_VALUE_HINTS.get(table, {})
            for table in (program.tables() if program else [])
            if RUN_VALUE_HINTS.get(table)
        },
    }

    if extra:

        state.update(
            extra
        )

    return state


# =============================================================================
# SOURCE
# =============================================================================

def choose_base_table(
    question: str,
    schema: SchemaInfo,
    rejected: list[str] | None = None,
) -> str:

    rejected = (
        rejected
        or []
    )

    all_tables = list(
        schema.tables.values()
    )

    allowed = [
        table

        for table
        in all_tables

        if (
            table.name
            not in rejected
        )
    ]

    if not allowed:

        allowed = (
            all_tables
        )

    candidates = []

    for table in allowed:

        description = {
            "table":
                table.name,

            "columns": [
                (
                    f"{column.name}:"
                    f"{column.kind}"
                )

                for column
                in table.columns
            ],

            "relationships": [
                fk.label()

                for fk
                in schema.foreign_keys

                if (
                    fk.from_table
                    == table.name

                    or

                    fk.to_table
                    == table.name
                )
            ],
        }

        candidates.append(
            (
                table.name,
                description,
            )
        )

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            None,
            "choose base table",
            {
                "previously_rejected_base_tables":
                    rejected,
            },
        ),

        (
            "Choose the table that is the best starting "
            "relation for answering the user question. "
            "This is only the starting table; related "
            "tables can be joined later. Prefer the table "
            "that directly contains the entities or "
            "measurements the question is fundamentally about."
        ),

        candidates,

        label=
            "BASE TABLE",
    )

    return selected


# =============================================================================
# OUTPUT EXPRESSIONS
# =============================================================================

def output_candidates(
    schema: SchemaInfo,
    program: QueryProgram,
) -> list[
    tuple[
        SelectExpr,
        Any,
    ]
]:

    existing = {
        output.label()

        for output
        in program.outputs
    }

    result: list[
        tuple[
            SelectExpr,
            Any,
        ]
    ] = []

    # Raw columns.
    for column in (
        table_columns(
            schema,
            program,
        )
    ):

        raw = SelectExpr(
            kind=
                "column",

            table=
                column.table,

            column=
                column.name,

            alias=(
                f"{column.table}_"
                f"{column.name}"
            ),
        )

        if (
            raw.label()
            not in existing
        ):

            result.append(
                (
                    raw,
                    {
                        "operation":
                            "return column",

                        "column":
                            column.ref,

                        "kind":
                            column.kind,
                    },
                )
            )

        # Date projections.
        if (
            column.kind
            == "date"
        ):

            for transform in (
                "year",
                "month",
                "day",
            ):

                expr = SelectExpr(
                    kind=
                        "column",

                    table=
                        column.table,

                    column=
                        column.name,

                    transform=
                        transform,

                    alias=(
                        f"{column.table}_"
                        f"{column.name}_"
                        f"{transform}"
                    ),
                )

                if (
                    expr.label()
                    not in existing
                ):

                    result.append(
                        (
                            expr,
                            {
                                "operation":
                                    "return transformed date column",

                                "column":
                                    column.ref,

                                "transform":
                                    transform,
                            },
                        )
                    )

    # COUNT rows.
    count_rows = SelectExpr(
        kind=
            "aggregate",

        func=
            "COUNT_ROWS",

        alias=
            "row_count",
    )

    if (
        count_rows.label()
        not in existing
    ):

        result.append(
            (
                count_rows,

                {
                    "operation":
                        "count rows in the current relation",
                },
            )
        )

    # Column aggregates.
    for column in (
        table_columns(
            schema,
            program,
        )
    ):

        count_nonnull = SelectExpr(
            kind=
                "aggregate",

            table=
                column.table,

            column=
                column.name,

            func=
                "COUNT",

            alias=(
                f"count_"
                f"{column.table}_"
                f"{column.name}"
            ),
        )

        count_distinct = SelectExpr(
            kind=
                "aggregate",

            table=
                column.table,

            column=
                column.name,

            func=
                "COUNT_DISTINCT",

            alias=(
                f"count_distinct_"
                f"{column.table}_"
                f"{column.name}"
            ),
        )

        for (
            expr,
            description,
        ) in (
            (
                count_nonnull,
                {
                    "operation":
                        "count non-null values",

                    "column":
                        column.ref,
                },
            ),

            (
                count_distinct,
                {
                    "operation":
                        "count distinct values",

                    "column":
                        column.ref,
                },
            ),
        ):

            # A primary key cannot be NULL or repeated within its table.
            # COUNT(pk) and COUNT DISTINCT pk add ambiguous aliases for
            # COUNT rows without adding a useful candidate operation.
            if column.pk and not program.joins:
                continue

            if (
                expr.label()
                not in existing
            ):

                result.append(
                    (
                        expr,
                        description,
                    )
                )

        if (
            column.kind
            == "number"
        ):

            for func in (
                "SUM",
                "AVG",
                "MIN",
                "MAX",
            ):

                expr = SelectExpr(
                    kind=
                        "aggregate",

                    table=
                        column.table,

                    column=
                        column.name,

                    func=
                        func,

                    alias=(
                        f"{func.lower()}_"
                        f"{column.table}_"
                        f"{column.name}"
                    ),
                )

                if (
                    expr.label()
                    not in existing
                ):

                    result.append(
                        (
                            expr,
                            {
                                "operation":
                                    func,

                                "column":
                                    column.ref,

                                "kind":
                                    column.kind,
                            },
                        )
                    )

    return result


def choose_output(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> None:

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose one output expression",
        ),

        (
            "Choose one additional output expression "
            "that the final result needs to RETURN in order to "
            "answer the user question. Choose a raw "
            "column when the user wants values or labels. "
            "Choose an aggregate when the user asks for "
            "a count, sum, average, minimum, maximum, "
            "or another summarized quantity. Do not add "
            "an expression just because it is related. "
            "Filters, sort keys, and extremum metrics can be used "
            "without returning them as extra result columns."
        ),

        output_candidates(
            schema,
            program,
        ),

        label=
            "OUTPUT",
    )

    program.outputs.append(
        selected
    )


# =============================================================================
# GROUPING
# =============================================================================

def group_candidates(
    schema: SchemaInfo,
    program: QueryProgram,
) -> list[
    tuple[
        GroupSpec,
        Any,
    ]
]:

    existing = {
        group.label()

        for group
        in program.groups
    }

    result = []

    for column in (
        table_columns(
            schema,
            program,
        )
    ):

        group = GroupSpec(
            table=
                column.table,

            column=
                column.name,
        )

        if (
            group.label()
            not in existing
        ):

            result.append(
                (
                    group,

                    {
                        "group by":
                            column.ref,

                        "kind":
                            column.kind,
                    },
                )
            )

        if (
            column.kind
            == "date"
        ):

            for transform in (
                "year",
                "month",
                "day",
            ):

                group = GroupSpec(
                    table=
                        column.table,

                    column=
                        column.name,

                    transform=
                        transform,
                )

                if (
                    group.label()
                    not in existing
                ):

                    result.append(
                        (
                            group,

                            {
                                "group by":
                                    column.ref,

                                "date grain":
                                    transform,
                            },
                        )
                    )

    return result


def choose_group(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> None:

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose grouping dimension",
        ),

        (
            "Choose the grouping dimension required "
            "by the question. Grouping changes the "
            "unit of the result, for example one "
            "result per country, product, year, or "
            "month. Choose only a grouping that the "
            "question actually needs."
        ),

        group_candidates(
            schema,
            program,
        ),

        label=
            "GROUP",
    )

    program.groups.append(
        selected
    )


# =============================================================================
# FILTERS
# =============================================================================

def available_filter_columns(question: str, schema: SchemaInfo,
                             program: QueryProgram) -> list[ColumnInfo]:
    user_numbers = {value for value in extract_question_literals(question)
                    if re.fullmatch(r"\d+(?:\.\d+)?", value)}
    return [column for column in table_columns(schema, program)
            if column.kind != "number" or user_numbers
            or (RUN_ORDINAL_VALUES.get(column.ref) and ordinal_reference_in_question(question))]


def choose_filter_column(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
    conn: sqlite3.Connection,
) -> ColumnInfo:

    candidates = [
        (
            column,
            {
                "column":
                    column.ref,

                "kind":
                    column.kind,
                "observed_suffix_patterns": text_suffix_patterns(conn, column),
                "observed_ordinal_values": RUN_ORDINAL_VALUES.get(column.ref, []),
            },
        )

        for column in available_filter_columns(question, schema, program)
    ]

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose filter column",
        ),

        (
            "Choose the column on which the next "
            "missing restriction from the user question "
            "should be applied. The current program "
            "already includes the listed filters, so "
            "choose a column for a condition that is "
            "still missing. Do not add a filter if the "
            "question does not require one; the planner "
            "should have avoided this stage in that case. "
            "Observed value suffixes can encode categories or statuses "
            "even when they are stored in a name field."
        ),

        candidates,

        label=
            "FILTER COLUMN",
    )

    return selected


def operator_candidates(
    column: ColumnInfo,
) -> list[
    tuple[
        str,
        Any,
    ]
]:

    if (
        column.kind
        == "number"
    ):

        operators = [
            "=",
            "!=",
            ">",
            ">=",
            "<",
            "<=",
        ]

    elif (
        column.kind
        == "date"
    ):

        operators = [
            "=",
            "!=",
            ">",
            ">=",
            "<",
            "<=",
            "YEAR=",
            "MONTH=",
        ]

    else:

        operators = [
            "=",
            "!=",
            "LIKE",
        ]

    if not column.notnull and not column.pk:
        operators.extend(["IS NULL", "IS NOT NULL"])

    return [
        (
            operator,

            {
                "operator":
                    operator,

                "column_kind":
                    column.kind,
            },
        )

        for operator
        in operators
    ]


def extract_question_literals(
    question: str,
) -> list[str]:

    found: list[str] = []

    # Quoted strings.
    for match in re.finditer(
        r"['\"]([^'\"]+)['\"]",
        question,
    ):

        found.append(
            match.group(1)
        )

    # ISO dates.
    found.extend(
        re.findall(
            r"\b\d{4}-\d{2}-\d{2}\b",
            question,
        )
    )

    # Natural month/year.
    months = {
        "january":
            1,

        "february":
            2,

        "march":
            3,

        "april":
            4,

        "may":
            5,

        "june":
            6,

        "july":
            7,

        "august":
            8,

        "september":
            9,

        "october":
            10,

        "november":
            11,

        "december":
            12,
    }

    lower_question = (
        question.lower()
    )

    for (
        name,
        number,
    ) in months.items():

        match = re.search(
            rf"\b{name}\b"
            rf"(?:\s+(\d{{4}}))?",
            lower_question,
        )

        if not match:
            continue

        if match.group(1):

            found.append(
                f"{match.group(1)}-"
                f"{number:02d}"
            )

        else:

            found.append(
                f"{number:02d}"
            )

    # Years and numeric constants.
    found.extend(
        re.findall(
            r"\b\d+(?:\.\d+)?\b",
            question,
        )
    )

    # Small number words are genuine user literals, not database values.
    number_words = {
        word: index for index, word in enumerate(
            "zero one two three four five six seven eight nine ten eleven "
            "twelve thirteen fourteen fifteen sixteen seventeen eighteen "
            "nineteen twenty".split()
        )
    }
    found.extend(str(number_words[word]) for word in re.findall(r"\b[a-z]+\b", lower_question)
                 if word in number_words)
    result = []
    seen = set()

    for value in found:

        if value in seen:
            continue

        seen.add(
            value
        )

        result.append(
            value
        )

    return result


def ordinal_literals(question: str) -> list[str]:
    numbers = {"first": 1, "second": 2, "third": 3, "fourth": 4,
               "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
               "ninth": 9, "tenth": 10}
    return [str(numbers[word]) for word in re.findall(r"\b[a-z]+\b", question.lower())
            if word in numbers]


def ordinal_reference_in_question(question: str) -> bool:
    return bool(ordinal_literals(question) or re.search(
        r"\b(original|initial|earliest|latest|newest|oldest)\b", question.lower()))


def is_ordinal_column(column: ColumnInfo) -> bool:
    name = column.name.lower()
    return bool(re.search(r"(^|_)(position|ordinal|sequence|index|rank|number)($|_)", name)
                or "_in_" in name)


def values_mentioned_in_question(
    conn: sqlite3.Connection,
    column: ColumnInfo,
    question: str,
) -> list[Any]:

    """
    Search one selected column for values whose textual
    representation already appears in the user's question.

    The rows are not sent to Jev. Only matching values become
    candidates later.
    """

    stats[
        "value_queries"
    ] += 1

    sql = (
        f"SELECT DISTINCT "
        f"{qident(column.name)} "
        f"FROM "
        f"{qident(column.table)} "
        f"WHERE "
        f"{qident(column.name)} "
        f"IS NOT NULL "
        f"AND "
        f"instr("
        f"lower(?), "
        f"lower("
        f"CAST("
        f"{qident(column.name)} "
        f"AS TEXT"
        f")"
        f")"
        f") > 0 "
        f"LIMIT "
        f"{MAX_DISTINCT_VALUES}"
    )

    return [
        row[0]

        for row
        in conn.execute(
            sql,
            (
                question,
            ),
        ).fetchall()
    ]


def related_values(
    conn: sqlite3.Connection,
    column: ColumnInfo,
    question: str,
) -> tuple[list[Any], list[str]]:
    """Inspect one chosen column for partial lexical matches, without sending rows."""
    if column.kind not in ("text", "date"):
        return [], []
    tokens = list(dict.fromkeys(
        token.lower() for token in re.findall(r"[\w]+", question)
        if len(token) >= 4 and token.lower() != RUN_DATABASE_NAME.lower()
    ))[:12]
    values: list[Any] = []
    for token in tokens:
        stats["value_queries"] += 1
        sql = (f"SELECT DISTINCT {qident(column.name)} FROM {qident(column.table)} "
               f"WHERE {qident(column.name)} IS NOT NULL "
               f"AND instr(lower(CAST({qident(column.name)} AS TEXT)), ?) > 0 "
               f"LIMIT {min(MAX_DISTINCT_VALUES, 24)}")
        values.extend(row[0] for row in conn.execute(sql, (token,)).fetchall())
    return list(dict.fromkeys(values))[:MAX_DISTINCT_VALUES], tokens


def text_suffix_patterns(conn: sqlite3.Connection, column: ColumnInfo) -> list[dict[str, Any]]:
    """Summarize repeated parenthesized suffixes in one text column."""
    if column.kind != "text":
        return []
    stats["value_queries"] += 1
    sql = (f"SELECT {qident(column.name)} FROM {qident(column.table)} "
           f"WHERE {qident(column.name)} IS NOT NULL LIMIT 2000")
    counts: Counter[str] = Counter()
    for (value,) in conn.execute(sql):
        match = re.search(r"\([^()]{2,30}\)\s*$", str(value))
        if match:
            counts[match.group().strip()] += 1
    return [{"suffix": suffix, "rows_sampled": count}
            for suffix, count in counts.most_common(5) if count >= 2]


def profile_categories(conn: sqlite3.Connection, schema: SchemaInfo) -> None:
    """Expose only small categorical value sets, never whole data columns."""
    for table in schema.tables.values():
        hints = {}
        for column in table.columns:
            if (column.kind == "number" and not column.pk
                    and column.name != "id" and not column.name.endswith("_id")):
                stats["value_queries"] += 1
                numeric = [row[0] for row in conn.execute(
                    f"SELECT DISTINCT {qident(column.name)} FROM "
                    f"(SELECT {qident(column.name)} FROM {qident(table.name)} "
                    f"WHERE {qident(column.name)} IS NOT NULL LIMIT 2000) LIMIT 51"
                )]
                if (3 <= len(numeric) <= 50 and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in numeric
                ) and min(numeric) == 1 and max(numeric) <= 50):
                    RUN_ORDINAL_VALUES[column.ref] = sorted(numeric)
                continue
            if column.kind not in ("text", "boolean"):
                continue
            stats["value_queries"] += 1
            values = [row[0] for row in conn.execute(
                f"SELECT DISTINCT {qident(column.name)} FROM "
                f"(SELECT {qident(column.name)} FROM {qident(table.name)} "
                f"WHERE {qident(column.name)} IS NOT NULL LIMIT 2000) LIMIT 13"
            )]
            if 1 < len(values) <= 12 and all(
                isinstance(value, (str, int, float, bool)) for value in values
            ):
                hints[column.name] = values
        if hints:
            RUN_VALUE_HINTS[table.name] = hints


def distinct_values(
    conn: sqlite3.Connection,
    column: ColumnInfo,
) -> list[Any]:

    stats[
        "value_queries"
    ] += 1

    sql = (
        f"SELECT DISTINCT "
        f"{qident(column.name)} "
        f"FROM "
        f"{qident(column.table)} "
        f"WHERE "
        f"{qident(column.name)} "
        f"IS NOT NULL "
        f"LIMIT "
        f"{MAX_DISTINCT_VALUES}"
    )

    return [
        row[0]

        for row
        in conn.execute(
            sql
        ).fetchall()
    ]


def coerce_literal(
    value: Any,
    kind: str,
) -> Any:

    if (
        kind
        == "number"
    ):

        try:

            number = float(
                value
            )

            if (
                number.is_integer()
            ):

                return int(
                    number
                )

            return number

        except Exception:

            return value

    return value


def choose_filter(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
    conn: sqlite3.Connection,
) -> None:

    column = (
        choose_filter_column(
            question,
            schema,
            program,
            conn,
        )
    )

    related, related_tokens = related_values(conn, column, question)
    suffix_patterns = text_suffix_patterns(conn, column)

    operator, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose filter operator",
            {
                "pending_filter_column":
                    column.ref,
                "related_column_values": related[:24],
                "observed_suffix_patterns": suffix_patterns,
                "operator_evidence": "Use LIKE for a category or phrase broader than exact stored values, including repeated status suffixes; use = for exact identity.",
            },
        ),

        (
            "Choose the comparison operator that "
            "expresses the user's restriction on "
            "the selected column."
        ),

        operator_candidates(
            column
        ),

        label=
            "FILTER OPERATOR",
    )

    if operator in ("IS NULL", "IS NOT NULL"):
        program.filters.append(FilterSpec(
            table=column.table, column=column.name,
            operator=operator, value=None,
        ))
        return

    values: list[Any] = []

    literals = (
        extract_question_literals(
            question
        )
    )
    if column.kind == "number" and is_ordinal_column(column):
        literals.extend(ordinal_literals(question))
    if column.kind == "number" and ordinal_reference_in_question(question):
        literals.extend(str(value) for value in RUN_ORDINAL_VALUES.get(column.ref, []))

    # LIKE must receive a pattern. A bare stored value under LIKE
    # silently becomes an exact match and misses broader categories.
    if (
        operator
        == "LIKE"
    ):

        values.extend(f"%{pattern['suffix']}" for pattern in suffix_patterns)

        for literal in literals:

            values.extend(
                [
                    f"%{literal}%",
                    f"{literal}%",
                    f"%{literal}",
                ]
            )
        for token in related_tokens:
            if any(token in str(value).lower() for value in related):
                values.append(f"%{token}%")
    else:
        values.extend(literals)
        values.extend(values_mentioned_in_question(conn, column, question))
        values.extend(related)

    # Only NOW do we inspect values from ONE column.
    #
    # We are not giving Jev database rows as general context.
    if operator != "LIKE":
        if column.kind in ("text", "date", "boolean"):
            values.extend(distinct_values(conn, column))
        # Numeric candidates must come from user literals, never from an
        # arbitrary observed year, identifier, or threshold.

    deduplicated = []
    seen = set()

    for value in values:

        value = (
            coerce_literal(
                value,
                column.kind,
            )
        )

        key = (
            stable_json(
                value
            )
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        deduplicated.append(
            value
        )

    if not deduplicated:

        raise RuntimeError(
            "Could not generate "
            "filter values for "
            f"{column.ref}"
        )

    value, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose filter value",
            {
                "pending_filter_column":
                    column.ref,

                "pending_filter_operator":
                    operator,

                "candidate_source": (
                    "User literals plus matching/"
                    "distinct values from only the "
                    "selected column."
                ),
            },
        ),

        (
            "Choose the concrete value that completes "
            "the restriction requested by the user. "
            "Use the original question, schema, and "
            "current program as context. Do not choose "
            "a value merely because it is common."
        ),

        [
            (
                candidate_value,

                {
                    "value":
                        candidate_value,

                    "column":
                        column.ref,

                    "operator":
                        operator,
                },
            )

            for candidate_value
            in deduplicated
        ],

        label=
            "FILTER VALUE",
    )

    transform = None

    actual_operator = (
        operator
    )

    # A year is a semantic date interval, not a date string equal to a
    # stored ISO day. Resolve interval boundaries mechanically after Jev
    # has selected the legal column, operator, and user-provided year.
    if column.kind == "date" and re.fullmatch(r"\d{4}", str(value)):
        year = str(value)
        if operator in ("=", "!="):
            transform = "year"
        elif operator in (">=", "<"):
            value = f"{year}-01-01"
        elif operator in ("<=", ">"):
            value = f"{year}-12-31"

    if (
        operator
        == "YEAR="
    ):

        transform = (
            "year"
        )

        actual_operator = (
            "="
        )

    elif (
        operator
        == "MONTH="
    ):

        transform = (
            "month"
        )

        actual_operator = (
            "="
        )

    selected_filter = FilterSpec(
            table=
                column.table,

            column=
                column.name,

            operator=
                actual_operator,

            value=
                value,

            transform=
                transform,
        )
    if selected_filter not in program.filters:
        program.filters.append(selected_filter)
        if operator == "LIKE":
            matched_pattern = next(
                (pattern for pattern in suffix_patterns
                 if str(value).endswith(pattern["suffix"])), None
            )
            if matched_pattern:
                DECISION_EVIDENCE.append({
                    "stage": "OBSERVED VALUE PATTERN",
                    "selected_filter": selected_filter.label(),
                    "observed_suffix": matched_pattern,
                    "meaning": "Jev selected a repeated database value suffix as evidence for the user's category; the final vet should assess this mapping.",
                })


# =============================================================================
# JOINS
# =============================================================================

def choose_join(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> None:

    joins = (
        available_joins(
            schema,
            program,
        )
    )

    if not joins:

        raise RuntimeError(
            "Planner requested ADD_JOIN "
            "but no executable foreign-key "
            "join is available"
        )

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose foreign-key join",
        ),

        (
            "Choose the foreign-key relationship "
            "that adds the table needed to answer "
            "the question. Every option is an "
            "executable relationship discovered "
            "from SQLite schema metadata. Choose "
            "a join only because the new table "
            "contributes required columns or "
            "constraints."
        ),

        [
            (
                join,

                {
                    "join":
                        join.label(),

                    "adds_table":
                        join.right_table,
                },
            )

            for join
            in joins
        ],

        label=
            "JOIN",
    )

    program.joins.append(
        selected
    )


# =============================================================================
# ORDER / EXTREMUM / LIMIT / DISTINCT
# =============================================================================

def selected_aliases(
    program: QueryProgram,
) -> list[
    tuple[
        str,
        str,
    ]
]:

    return [
        (
            (
                output.alias
                or output.label()
            ),

            output.label(),
        )

        for output
        in program.outputs
    ]


def choose_having(question: str, schema: SchemaInfo, program: QueryProgram) -> None:
    """Choose a restriction on a grouped result, never on source rows."""
    aggregates = [output for output in program.outputs if output.kind == "aggregate"]
    literals = []
    for value in extract_question_literals(question):
        try:
            literals.append(float(value) if "." in value else int(value))
        except ValueError:
            continue
    literals = list(dict.fromkeys(literals))
    candidates = [
        (HavingSpec(output.alias or output.label(), op, value),
         {"aggregate": output.label(), "operator": op, "threshold": value})
        for output in aggregates
        for op in ("=", "!=", ">", ">=", "<", "<=")
        for value in literals
        if HavingSpec(output.alias or output.label(), op, value) not in program.having
    ]
    if not candidates:
        raise RuntimeError("No user-provided numeric threshold for a grouped condition")
    selected, _ = choice(
        compiler_state(question, schema, program, "choose grouped result condition"),
        "Choose the aggregate threshold that restricts groups in the user's request. "
        "This condition applies after GROUP BY, not to individual source rows.",
        candidates, label="HAVING",
    )
    program.having.append(selected)


def choose_order(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> None:

    candidates = []

    for (
        alias,
        semantic_label,
    ) in selected_aliases(
        program
    ):

        if any(order.key == alias for order in program.order_by):
            continue

        for direction in (
            "ASC",
            "DESC",
        ):

            candidates.append(
                (
                    OrderSpec(
                        key=
                            alias,

                        direction=
                            direction,
                    ),

                    {
                        "sort":
                            semantic_label,

                        "direction":
                            direction,
                    },
                )
            )

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose ordering",
        ),

        (
            "Choose the ordering required by the user's "
            "wording. Consider highest, lowest, earliest, "
            "latest, alphabetical, ascending, and descending. "
            "The sort key must be one of the output expressions "
            "already present. Existing sort keys take priority; this "
            "new key resolves ties after them."
        ),

        candidates,

        label=
            "ORDER",
    )

    program.order_by.append(selected)


def revise_order_key(question: str, schema: SchemaInfo,
                     program: QueryProgram, vet: dict[str, Any]) -> None:
    """Remove a wrong sort key while preserving useful tie breakers."""
    candidates = [
        (index, {"revision": "remove sort key", "key": order.key,
                 "direction": order.direction,
                 "remaining": [f"{item.key} {item.direction}" for pos, item in enumerate(program.order_by)
                               if pos != index]})
        for index, order in enumerate(program.order_by)
    ]
    selected, _ = choice(
        compiler_state(question, schema, program, "revise rejected ordering",
                       {"last_vet": vet}),
        "The final vet rejected the ordering. Choose the one sort key that "
        "does not match the user's requested priority, keeping the other key.",
        candidates, label="ORDER REVISION",
    )
    del program.order_by[selected]


def choose_extremum(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> None:

    candidates = []

    for (
        alias,
        semantic_label,
    ) in selected_aliases(
        program
    ):

        candidates.append(
            (
                {
                    "key":
                        alias,

                    "mode":
                        "max",
                },

                {
                    "operation":
                        "ARGMAX with ties",

                    "on":
                        semantic_label,
                },
            )
        )

        candidates.append(
            (
                {"key": alias, "mode": "min"},
                {"operation": "ARGMIN with ties", "on": semantic_label},
            )
        )

    # An extremum may determine *which rows* to return without itself being
    # a requested output (e.g. names at the largest salary). Keep that
    # metric private to the compiled CTE, so result columns stay exact.
    if not has_aggregate(program) and not program.groups:
        for table_name in program.tables():
            table = schema.tables[table_name]
            for column in table.columns:
                if column.kind not in ("number", "date"):
                    continue
                for mode in ("max", "min"):
                    candidates.append((
                        {"key": "__extremum_metric", "mode": mode,
                         "source_table": table_name, "source_column": column.name},
                        {"operation": "ARGMAX with ties" if mode == "max" else "ARGMIN with ties",
                         "on": f"{table_name}.{column.name}", "output": "not returned"},
                    ))

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose result extremum",
        ),

        (
            "Choose the result reduction that matches "
            "the question. ARGMAX returns every row tied "
            "for the greatest value of the chosen output. "
            "ARGMIN returns every row tied for the smallest "
            "value. Use this for questions such as which "
            "item had the most, highest, least, or lowest value. "
            "A metric can select rows without being returned."
        ),

        candidates,

        label=
            "EXTREMUM",
    )

    program.extremum = (
        selected
    )


def choose_limit(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> None:

    numbers = []

    for token in re.findall(
        r"\b\d+\b",
        question,
    ):

        try:

            number = int(
                token
            )

        except Exception:

            continue

        if (
            0
            < number
            <= 10000
        ):

            numbers.append(
                number
            )

    numbers.extend(
        [
            1,
            5,
            10,
            20,
        ]
    )

    values = []
    seen = set()

    for number in numbers:

        if number in seen:
            continue

        seen.add(
            number
        )

        values.append(
            number
        )

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose result limit",
        ),

        (
            "Choose how many rows the user explicitly "
            "wants returned. Prefer a number stated in "
            "the question. If the user asks for one best "
            "row but ties must be preserved, the planner "
            "should use ARGMAX or ARGMIN instead of LIMIT 1."
        ),

        [
            (
                number,

                {
                    "limit":
                        number,
                },
            )

            for number
            in values
        ],

        label=
            "LIMIT",
    )

    program.limit = (
        selected
    )


def choose_distinct(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> None:

    selected, _ = choice(
        compiler_state(
            question,
            schema,
            program,
            "choose distinctness",
        ),

        (
            "Decide whether the final rows should be "
            "DISTINCT. Choose DISTINCT only when duplicate "
            "rows would not represent distinct answers "
            "requested by the user."
        ),

        [
            (
                True,
                "DISTINCT rows",
            ),

            (
                False,
                "keep duplicate rows",
            ),
        ],

        label=
            "DISTINCT",
    )

    program.distinct = (
        selected
    )

    program.distinct_decided = (
        True
    )


# =============================================================================
# SQL COMPILER
# =============================================================================

def alias_map(
    program: QueryProgram,
) -> dict[
    str,
    str,
]:

    return {
        table:
            f"t{i}"

        for i, table
        in enumerate(
            program.tables()
        )
    }


def column_sql(
    table: str,
    column: str,
    aliases: dict[str, str],
) -> str:

    return (
        f"{qident(aliases[table])}."
        f"{qident(column)}"
    )


def transformed_sql(
    expression: str,
    transform: str | None,
) -> str:

    if (
        transform
        == "year"
    ):

        return (
            f"strftime("
            f"'%Y', "
            f"{expression}"
            f")"
        )

    if (
        transform
        == "month"
    ):

        return (
            f"strftime("
            f"'%Y-%m', "
            f"{expression}"
            f")"
        )

    if (
        transform
        == "day"
    ):

        return (
            f"date("
            f"{expression}"
            f")"
        )

    return expression


def compile_base_sql(
    program: QueryProgram,
) -> tuple[
    str,
    list[Any],
]:

    aliases = (
        alias_map(
            program
        )
    )

    params: list[Any] = []

    select_sql = []

    for output in (
        program.outputs
    ):

        alias = qident(
            output.alias
            or output.label()
        )

        if (
            output.kind
            == "column"
        ):

            expression = column_sql(
                output.table,
                output.column,
                aliases,
            )

            expression = (
                transformed_sql(
                    expression,
                    output.transform,
                )
            )

        elif (
            output.func
            == "COUNT_ROWS"
        ):

            expression = (
                "COUNT(*)"
            )

        elif (
            output.func
            == "COUNT_DISTINCT"
        ):

            expression = (
                "COUNT(DISTINCT "
                + column_sql(
                    output.table,
                    output.column,
                    aliases,
                )
                + ")"
            )

        else:

            expression = (
                f"{output.func}("
                + column_sql(
                    output.table,
                    output.column,
                    aliases,
                )
                + ")"
            )

        select_sql.append(
            f"{expression} "
            f"AS "
            f"{alias}"
        )

    if program.extremum and program.extremum.get("source_table"):
        hidden = column_sql(
            program.extremum["source_table"],
            program.extremum["source_column"],
            aliases,
        )
        select_sql.append(f"{hidden} AS {qident('__extremum_metric')}")

    if not select_sql:

        raise RuntimeError(
            "Cannot compile a query "
            "with no outputs"
        )

    distinct = (
        "DISTINCT "

        if program.distinct

        else ""
    )

    base_alias = (
        aliases[
            program.base_table
        ]
    )

    sql = (
        f"SELECT "
        f"{distinct}"
        f"{', '.join(select_sql)} "
        f"FROM "
        f"{qident(program.base_table)} "
        f"AS "
        f"{qident(base_alias)}"
    )

    for join in (
        program.joins
    ):

        right_alias = (
            aliases[
                join.right_table
            ]
        )

        left_expression = (
            column_sql(
                join.left_table,
                join.left_column,
                aliases,
            )
        )

        right_expression = (
            column_sql(
                join.right_table,
                join.right_column,
                aliases,
            )
        )

        sql += (
            f" JOIN "
            f"{qident(join.right_table)} "
            f"AS "
            f"{qident(right_alias)} "
            f"ON "
            f"{left_expression} "
            f"= "
            f"{right_expression}"
        )

        for extra_left_table, extra_left_column, extra_right_table, extra_right_column in join.extra_on:
            sql += (
                " AND "
                + column_sql(extra_left_table, extra_left_column, aliases)
                + " = "
                + column_sql(extra_right_table, extra_right_column, aliases)
            )

    if program.filters:

        clauses = []

        for filter_ in (
            program.filters
        ):

            left = (
                column_sql(
                    filter_.table,
                    filter_.column,
                    aliases,
                )
            )

            left = (
                transformed_sql(
                    left,
                    filter_.transform,
                )
            )

            if filter_.operator in ("IS NULL", "IS NOT NULL"):
                clauses.append(f"{left} {filter_.operator}")
            else:
                clauses.append(f"{left} {filter_.operator} ?")
                params.append(filter_.value)

        sql += (
            " WHERE "
            + " AND ".join(
                clauses
            )
        )

    if program.groups:

        expressions = []

        for group in (
            program.groups
        ):

            expression = (
                column_sql(
                    group.table,
                    group.column,
                    aliases,
                )
            )

            expression = (
                transformed_sql(
                    expression,
                    group.transform,
                )
            )

            expressions.append(
                expression
            )

        sql += (
            " GROUP BY "
            + ", ".join(
                expressions
            )
        )

    if program.having:
        sql += " HAVING " + " AND ".join(
            f"{qident(condition.key)} {condition.operator} ?"
            for condition in program.having
        )
        params.extend(condition.value for condition in program.having)

    if program.order_by:

        sql += (
            " ORDER BY "
            + ", ".join(
                (
                    f"{qident(order.key)} "
                    f"{order.direction}"
                )

                for order
                in program.order_by
            )
        )

    if (
        program.limit
        is not None
    ):

        sql += (
            " LIMIT "
            + str(
                int(
                    program.limit
                )
            )
        )

    return (
        sql,
        params,
    )


def compile_sql(
    program: QueryProgram,
) -> tuple[
    str,
    list[Any],
]:

    base_sql, params = (
        compile_base_sql(
            program
        )
    )

    if not program.extremum:

        return (
            base_sql,
            params,
        )

    key = qident(
        program.extremum[
            "key"
        ]
    )

    aggregate = (
        "MAX"

        if (
            program.extremum[
                "mode"
            ]
            == "max"
        )

        else "MIN"
    )

    # Tie preserving.
    #
    # If two products both have 32 orders,
    # ARGMAX returns both.
    visible_columns = (
        ", ".join(qident(output.alias or output.label()) for output in program.outputs)
        if program.extremum.get("source_table") else "*"
    )
    sql = (
        f"WITH base AS ("
        f"{base_sql}"
        f") "
        f"SELECT {visible_columns} "
        f"FROM base "
        f"WHERE "
        f"{key} = "
        f"("
        f"SELECT "
        f"{aggregate}("
        f"{key}"
        f") "
        f"FROM base"
        f")"
    )

    return (
        sql,
        params,
    )


# =============================================================================
# SEMANTIC VET
# =============================================================================

def vet_query(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
    sql: str,
) -> tuple[
    bool,
    str,
    dict[str, Any],
]:

    stats[
        "vet_calls"
    ] += 1

    state = compiler_state(
        question,
        schema,
        program,
        "final semantic vet before SQL execution",
        {
            "candidate_sql":
                sql,
            "bound_parameters": [filter_.value for filter_ in program.filters],

            "execution_policy": (
                "This candidate SELECT will not be "
                "executed unless this semantic vet passes. "
                "The database connection is read-only."
            ),
            "relational_rules": [
                "A sole aggregate output without GROUP BY computes one overall value; GROUP BY is for per-category results.",
                "A filter on a stored ordinal equal to one can identify the first item within every group without GROUP BY.",
                "Additional restrictions not stated by the user can change the intended answer even if they happen to leave these data unchanged.",
                "Naming an entity or table does not imply a subtype filter unless the user requests that subtype.",
            ],
        },
    )

    # One shared State.
    #
    # Several independent Jev questions inspect the same finished
    # relational program and SQL query.
    response = system_one(
        state,
        {
            "answers_question": {
                "type":
                    "noul",

                "instructions": (
                    "Would the rows produced by this exact "
                    "query directly answer the user's question?"
                ),

                "criteria": {
                    "true": (
                        "The selected outputs and final result "
                        "shape directly answer what was asked."
                    ),

                    "false": (
                        "The query may be related or partially "
                        "useful, but its rows would not directly "
                        "answer the question."
                    ),
                },
            },

            "constraints_complete": {
                "type":
                    "noul",

                "instructions": (
                    "Does this query include every explicit "
                    "restriction in the user's question that "
                    "can be represented with this schema?"
                ),

                "criteria": {
                    "true": (
                        "All required filters, joins, time "
                        "restrictions, and other stated "
                        "constraints are represented."
                    ),

                    "false": (
                        "At least one explicit condition from "
                        "the question is missing or incorrectly "
                        "represented."
                    ),
                },
            },

            "relational_logic_correct": {
                "type":
                    "noul",

                "instructions": (
                    "Are the joins, selected outputs, "
                    "aggregation, grouping, distinctness, "
                    "ordering, limit, and extremum behavior "
                    "semantically correct for the question?"
                ),

                "criteria": {
                    "true": (
                        "The relational operations compute "
                        "the quantity and result grain that "
                        "the user requested."
                    ),

                    "false": (
                        "At least one relational operation "
                        "changes the meaning incorrectly, "
                        "loses required information, or "
                        "produces the wrong result grain."
                    ),
                },
            },

            "verdict": {
                "type":
                    "choice",

                "instructions": (
                    "Choose what the compiler should do BEFORE "
                    "execution. PASS only if this exact candidate "
                    "query is already semantically complete. "
                    "Otherwise identify the part that most needs "
                    "revision."
                ),

                "criteria": {
                    "PASS": (
                        "Execute this exact read-only query."
                    ),

                    "REVISE_SOURCE_OR_JOINS": (
                        "The starting table or joined "
                        "tables/relationships are wrong "
                        "or incomplete."
                    ),

                    "REVISE_OUTPUTS": (
                        "The returned columns or aggregate "
                        "expressions are wrong or incomplete."
                    ),

                    "REVISE_FILTERS": (
                        "Required restrictions are missing "
                        "or incorrect."
                    ),

                    "REVISE_GROUPING": (
                        "The grouping or result grain is "
                        "wrong or incomplete."
                    ),

                    "REVISE_ORDER_OR_REDUCTION": (
                        "The query needs different ordering, "
                        "LIMIT, ARGMAX, or ARGMIN behavior."
                    ),

                    "UNSUPPORTED": (
                        "The question cannot be answered "
                        "with this schema using the currently "
                        "supported relational operations."
                    ),
                },
            },
        },
    )

    answers = (
        response[
            "answers"
        ]
    )

    checks = {
        "answers_question":
            float(
                answers[
                    "answers_question"
                ][
                    "noul"
                ]
            ),

        "constraints_complete":
            float(
                answers[
                    "constraints_complete"
                ][
                    "noul"
                ]
            ),

        "relational_logic_correct":
            float(
                answers[
                    "relational_logic_correct"
                ][
                    "noul"
                ]
            ),
    }
    verdict = (
        answers[
            "verdict"
        ][
            "choice"
        ]
    )

    verdict_probability = float(
        answers[
            "verdict"
        ]
        .get(
            "probabilities",
            {},
        )
        .get(
            verdict,
            0.0,
        )
    )

    verdict_confidence = float(
        answers[
            "verdict"
        ]
        .get(
            "confidence",
            0.0,
        )
    )

    recheck = None
    weakest = min(checks, key=checks.get)
    borderline_pass = verdict == "PASS" and VET_THRESHOLD - 0.10 <= checks[weakest] < VET_THRESHOLD
    global_scalar_aggregate = (has_aggregate(program) and not program.groups
                               and all(output.kind == "aggregate" for output in program.outputs))
    grouping_without_aggregate = verdict == "REVISE_GROUPING" and not has_aggregate(program)
    grouping_not_needed = verdict == "REVISE_GROUPING" and global_scalar_aggregate
    contradictory_revision = (verdict != "PASS" and verdict != "UNSUPPORTED"
                              and (min(checks.values()) >= 0.78 or grouping_without_aggregate
                                   or grouping_not_needed))
    if borderline_pass or contradictory_revision:
        recheck = system_one(
            {**state, "stage": "independent review of inconsistent final vet",
             "first_vet": {"verdict": verdict, "checks": checks},
             "focus": weakest if borderline_pass else "verdict vs independent checks",
             "relational_note": (
                 "A WHERE condition on a stored ordinal can identify the first item "
                 "in each group without GROUP BY. Grouping without an aggregate "
                 "can discard valid rows in SQLite."
             ) if grouping_without_aggregate else (
                 "A sole aggregate without GROUP BY already computes a single overall value; "
                 "GROUP BY would change the requested result grain."
             ) if grouping_not_needed else None},
            {
                "focused_check": {
                    "type": "noul",
                    "instructions": (
                        f"Independently verify {weakest.replace('_', ' ')} for the exact program and SQL. "
                        "Consider every explicit phrase in the user request and the bound values."
                    ),
                    "criteria": {"true": "The exact candidate satisfies this check.",
                                 "false": "The exact candidate fails this check."},
                },
                "final_verdict": {
                    "type": "choice",
                    "instructions": "Would executing this exact candidate answer the request correctly, with no missing or wrong operations?",
                    "criteria": {"PASS": "Yes, execute the exact candidate.",
                                 "REVISE": "No, it needs correction."},
                },
            },
        )["answers"]
        recheck_pass = (recheck["final_verdict"]["choice"] == "PASS" and
                        float(recheck["focused_check"]["noul"]) >= VET_THRESHOLD)
        if recheck_pass:
            checks[weakest] = max(checks[weakest], float(recheck["focused_check"]["noul"]))
            if contradictory_revision:
                verdict = "PASS"
        elif borderline_pass:
            verdict = "REVISE_OUTPUTS" if weakest == "answers_question" else (
                "REVISE_FILTERS" if weakest == "constraints_complete" else "REVISE_GROUPING")
        if recheck_pass and verdict == "PASS":
            final_answer = recheck["final_verdict"]
            verdict_probability = float(final_answer.get("probabilities", {}).get("PASS", verdict_probability))
            verdict_confidence = float(final_answer.get("confidence", verdict_confidence))

    pattern_evidence = next((item for item in reversed(DECISION_EVIDENCE)
                             if item.get("stage") == "OBSERVED VALUE PATTERN"
                             and item.get("selected_filter") in
                             {filter_.label() for filter_ in program.filters}), None)
    if verdict != "PASS" and pattern_evidence:
        pattern_review = system_one(
            {**state, "stage": "independent review of observed value pattern",
             "observed_pattern_evidence": pattern_evidence,
             "first_vet": {"verdict": verdict, "checks": checks}},
            {
                "meaning_match": {
                    "type": "noul",
                    "instructions": "Does this observed suffix reasonably encode the user's intended category or status? Infer common abbreviations from the actual data pattern; do not demand literal wording.",
                    "criteria": {"true": "The chosen suffix represents the requested condition.",
                                 "false": "The suffix does not represent it."},
                },
                "result_complete": {
                    "type": "noul",
                    "instructions": "If the observed suffix has that meaning, would the exact selected columns and SQL return the requested answer with no omitted conditions?",
                    "criteria": {"true": "The exact candidate answers the request.",
                                 "false": "Something is still missing or incorrect."},
                },
                "final_verdict": {
                    "type": "choice",
                    "instructions": "Should this exact read-only candidate run?",
                    "criteria": {"PASS": "Both the value interpretation and complete result are correct.",
                                 "REVISE": "The candidate remains uncertain or wrong."},
                },
            },
        )["answers"]
        recheck = pattern_review
        if (pattern_review["final_verdict"]["choice"] == "PASS"
                and float(pattern_review["meaning_match"]["noul"]) >= VET_THRESHOLD
                and float(pattern_review["result_complete"]["noul"]) >= VET_THRESHOLD):
            verdict = "PASS"
            checks = {key: max(value, float(pattern_review["result_complete"]["noul"]))
                      for key, value in checks.items()}
            verdict_probability = float(pattern_review["final_verdict"].get("probabilities", {}).get("PASS", 0))
            verdict_confidence = float(pattern_review["final_verdict"].get("confidence", 0))
    emit("vet", checks=checks, verdict={"choice": verdict}, sql=sql,
         recheck=recheck)

    trace(
        "VET",
        verdict,
        verdict_probability,
        verdict_confidence,
        [
            (
                name,
                probability,
            )

            for (
                name,
                probability,
            ) in checks.items()
        ],
    )

    passed = (
        verdict
        == "PASS"

        and

        min(
            checks.values()
        )
        >= VET_THRESHOLD
    )
    DECISION_EVIDENCE.append({
        "stage": "FINAL VET",
        "selected": verdict,
        "checks": checks,
        "passed": passed,
        "candidate_program": program.state(),
    })

    # If verdict says PASS but one independent check is weak,
    # do not execute.
    if (
        verdict
        == "PASS"

        and

        not passed
    ):

        weakest = min(
            checks,
            key=
                checks.get,
        )

        if (
            weakest
            == "constraints_complete"
        ):

            verdict = (
                "REVISE_FILTERS"
            )

        elif (
            weakest
            == "relational_logic_correct"
        ):

            verdict = (
                "REVISE_GROUPING"
            )

        else:

            verdict = (
                "REVISE_OUTPUTS"
            )

    return (
        passed,
        verdict,
        {
            "checks":
                checks,
            "passed": passed,
            "verdict": verdict,

            "raw":
                answers,
            "recheck": recheck,
        },
    )


def apply_revision(
    program: QueryProgram,
    verdict: str,
) -> QueryProgram | str | None:

    if (
        verdict
        == "UNSUPPORTED"
    ):

        return None

    if (
        verdict
        == "REVISE_SOURCE_OR_JOINS"
    ):

        return (
            "RESELECT_SOURCE"
        )

    if (
        verdict
        == "REVISE_OUTPUTS"
    ):

        program.outputs.clear()
        program.groups.clear()
        program.order_by.clear()

        program.limit = None

        program.distinct = False
        program.distinct_decided = False

        program.extremum = None

    elif (
        verdict
        == "REVISE_FILTERS"
    ):

        program.filters.clear()
        program.having.clear()

    elif (
        verdict
        == "REVISE_GROUPING"
    ):

        program.groups.clear()
        program.order_by.clear()

        program.limit = None
        program.extremum = None

    elif (
        verdict
        == "REVISE_ORDER_OR_REDUCTION"
    ):

        program.order_by.clear()

        program.limit = None
        program.extremum = None

    return program


# =============================================================================
# PLANNER
# =============================================================================

def has_aggregate(
    program: QueryProgram,
) -> bool:

    return any(
        output.kind
        == "aggregate"

        for output
        in program.outputs
    )


def program_structurally_ready(
    program: QueryProgram,
) -> bool:

    if not program.outputs:

        return False

    # Prevent SQLite's permissive GROUP BY behavior from hiding
    # an incomplete relational program.
    if has_aggregate(
        program
    ):

        grouped = {
            (
                group.table,
                group.column,
                group.transform,
            )

            for group
            in program.groups
        }

        for output in (
            program.outputs
        ):

            if (
                output.kind
                == "column"

                and

                (
                    output.table,
                    output.column,
                    output.transform,
                )
                not in grouped
            ):

                return False

    aliases = {
        (
            output.alias
            or output.label()
        )
        for output
        in program.outputs
    }

    if any(
        order.key
        not in aliases

        for order
        in program.order_by
    ):

        return False

    if (
        program.extremum
        and program.extremum.get("key") not in aliases
        and not (program.extremum.get("key") == "__extremum_metric"
                 and program.extremum.get("source_table")
                 and program.extremum.get("source_column"))
    ):

        return False

    return True


def legal_actions(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> list[
    tuple[
        str,
        Any,
    ]
]:

    actions: list[
        tuple[
            str,
            Any,
        ]
    ] = []

    if available_joins(
        schema,
        program,
    ):

        actions.append(
            (
                "ADD_JOIN",

                (
                    "Add one schema-declared foreign-key "
                    "join because another table is needed."
                ),
            )
        )

    if len(program.outputs) < MAX_OUTPUTS:
        actions.append((
            "ADD_OUTPUT",
            "Add one column or aggregate that the user wants returned.",
        ))

    if available_filter_columns(question, schema, program):
        actions.append(("ADD_FILTER", "Add one missing WHERE restriction from the user's question."))

    if has_aggregate(
        program
    ):

        actions.append(
            (
                "ADD_GROUP",

                (
                    "Add one GROUP BY dimension because "
                    "the aggregate must be computed "
                    "separately per category or time bucket."
                ),
            )
        )

        if program.groups and len(program.having) < 3:
            actions.append((
                "ADD_HAVING",
                "Add a missing condition on a grouped aggregate, such as count or average.",
            ))

    if program.outputs:

        if not (
            program
            .distinct_decided
        ):

            actions.append(
                (
                    "SET_DISTINCT",

                    (
                        "Choose whether duplicate result "
                        "rows should be removed."
                    ),
                )
            )

        if (
            not program.extremum
            and len(program.order_by) < 3
            and len(program.order_by) < len(program.outputs)
        ):

            actions.append(
                (
                    "SET_ORDER",

                    (
                        "Add one requested sort key after existing keys; "
                        "use another key only for an explicit tie break."
                    ),
                )
            )

        if (
            program.extremum
            is None

            and

            not program.order_by

            and

            program.limit
            is None
        ):

            actions.append(
                (
                    "SET_EXTREMUM",

                    (
                        "Reduce to all rows tied for the "
                        "maximum or minimum selected "
                        "output value."
                    ),
                )
            )

        if (
            program.limit
            is None

            and

            program.extremum
            is None
        ):

            actions.append(
                (
                    "SET_LIMIT",

                    (
                        "Limit the number of returned rows."
                    ),
                )
            )

        if program_structurally_ready(
            program
        ):

            actions.append(
                (
                    "VET_QUERY",

                    (
                        "The relational program is structurally "
                        "valid. Compile SQL and run the Jev "
                        "semantic vet before execution."
                    ),
                )
            )

    return actions


def choose_next_action(
    question: str,
    schema: SchemaInfo,
    program: QueryProgram,
) -> str:

    stats[
        "planner_calls"
    ] += 1

    actions = legal_actions(question, schema, program)
    criteria = {f"a{i}": description for i, (_, description) in enumerate(actions)}
    response = system_one(
        compiler_state(question, schema, program, "choose next compiler action"),
        {
            "next_action": {
                "type": "choice",
                "instructions": (
                    "Choose the single next operation needed for the exact answer. "
                    "Only add a filter if the user explicitly narrows rows by a value, "
                    "time, category, or condition not yet in the program. A database "
                    "name, table name, or general subject is not a row filter. "
                    "Prefer VET_QUERY once all requested parts are present."
                ),
                "criteria": criteria,
            },
            "missing_filter": {
                "type": "noul",
                "instructions": (
                    "Does the user explicitly require another row-level WHERE condition "
                    "that is absent from the current program? Naming the database, "
                    "table, or general subject alone does not count."
                ),
                "criteria": {
                    "true": "A required row restriction is still absent.",
                    "false": "No additional row restriction is required."
                },
            },
            "missing_ordinal_filter": {
                "type": "noul",
                "instructions": (
                    "Does the request identify a row by its ordinal position "
                    "within a group (such as the first or Nth item), and does "
                    "the inspected schema have a stored position/index column "
                    "that has not yet been filtered? If so, this is a row "
                    "restriction, not a global minimum/maximum aggregate."
                ),
                "criteria": {
                    "true": "A stored ordinal column still needs a row filter.",
                    "false": "No such positional filter is missing."
                },
            },
            "missing_group_condition": {
                "type": "noul",
                "instructions": (
                    "Does the user require a numeric condition on each grouped "
                    "aggregate result (count, sum, average, etc.) that is not "
                    "yet represented by HAVING? Distinguish this from a WHERE "
                    "condition on individual source rows."
                ),
                "criteria": {"true": "A grouped aggregate condition is missing.",
                             "false": "No grouped aggregate condition is missing."},
            },
            "missing_extremum": {
                "type": "noul",
                "instructions": (
                    "Does the request ask for row(s) at a maximum or minimum metric "
                    "within the requested scope, while the current program lacks "
                    "that reduction? A top-N list with an explicit N instead needs "
                    "ORDER BY and LIMIT. Do not guess the maximum as a literal filter."
                ),
                "criteria": {"true": "A tie-preserving maximum/minimum row reduction is missing.",
                             "false": "No such extremum reduction is missing."},
            },
        },
    )
    answer = response["answers"]["next_action"]
    key = answer["choice"]
    index = int(key[1:])
    if not 0 <= index < len(actions):
        raise RuntimeError("Jev selected an invalid planner action")
    selected = actions[index][0]
    missing_filter = float(response["answers"]["missing_filter"]["noul"])
    missing_ordinal_filter = float(response["answers"]["missing_ordinal_filter"]["noul"])
    missing_group_condition = float(response["answers"]["missing_group_condition"]["noul"])
    missing_extremum = float(response["answers"]["missing_extremum"]["noul"])
    if missing_group_condition >= 0.5 and any(name == "ADD_HAVING" for name, _ in actions):
        selected = "ADD_HAVING"
    if (missing_extremum >= 0.5 and selected not in ("ADD_JOIN", "ADD_OUTPUT", "ADD_HAVING")
            and any(name == "SET_EXTREMUM" for name, _ in actions)):
        selected = "SET_EXTREMUM"
    if missing_ordinal_filter >= 0.5 and any(name == "ADD_FILTER" for name, _ in actions):
        selected = "ADD_FILTER"
        missing_filter = max(missing_filter, missing_ordinal_filter)
    if selected == "ADD_FILTER" and missing_filter < 0.5:
        if any(name == "VET_QUERY" for name, _ in actions):
            selected = "VET_QUERY"
        else:
            non_filter = [(i, probability) for i, probability in answer.get("probabilities", {}).items()
                          if actions[int(i[1:])][0] != "ADD_FILTER"]
            if non_filter:
                selected = actions[int(max(non_filter, key=lambda pair: pair[1])[0][1:])][0]
    probability = float(answer.get("probabilities", {}).get(key, 0.0))
    confidence = float(answer.get("confidence", 0.0))
    ranking = sorted(((str(description), float(answer.get("probabilities", {}).get(f"a{i}", 0.0)))
                      for i, (_, description) in enumerate(actions)), key=lambda pair: pair[1], reverse=True)
    trace("PLANNER", selected, probability, confidence, ranking[:6])
    DECISION_EVIDENCE.append({"stage": "PLANNER", "selected": selected,
                              "probability": probability, "confidence": confidence,
                              "missing_filter": missing_filter,
                              "missing_ordinal_filter": missing_ordinal_filter,
                              "missing_group_condition": missing_group_condition,
                              "missing_extremum": missing_extremum})
    emit("decision", label="PLANNER", selected=selected, probability=probability,
         confidence=confidence,
         candidates=[{"label": name, "probability": float(answer.get("probabilities", {}).get(f"a{i}", 0.0))}
                     for i, (name, _) in enumerate(actions)], automatic=False)
    return selected


# =============================================================================
# STATIC SAFETY + EXECUTION
# =============================================================================

def static_safety_check(
    sql: str,
) -> None:

    compact = re.sub(
        r"\s+",
        " ",
        sql.strip(),
    ).upper()

    if not (
        compact.startswith(
            "SELECT "
        )
        or
        compact.startswith(
            "WITH "
        )
    ):

        raise RuntimeError(
            "Refusing non-SELECT SQL"
        )

    forbidden = [
        " INSERT ",
        " UPDATE ",
        " DELETE ",
        " DROP ",
        " ALTER ",
        " ATTACH ",
        " DETACH ",
        " PRAGMA ",
        " REPLACE ",
        " VACUUM ",
    ]

    padded = (
        " "
        + compact
        + " "
    )

    if any(
        token in padded

        for token
        in forbidden
    ):

        raise RuntimeError(
            "Refusing SQL containing "
            "a write/admin statement"
        )


def explain_only(
    conn: sqlite3.Connection,
    sql: str,
    params: list[Any],
) -> None:

    # This happens AFTER the Jev semantic vet.
    #
    # It asks SQLite to parse/plan the query,
    # but does not run the SELECT result scan.
    conn.execute(
        "EXPLAIN QUERY PLAN "
        + sql,
        params,
    ).fetchall()


def execute_query(
    conn: sqlite3.Connection,
    sql: str,
    params: list[Any],
) -> tuple[
    list[str],
    list[
        tuple[Any, ...]
    ],
    bool,
    int,
]:

    stats[
        "sql_executions"
    ] += 1

    cursor = conn.execute(
        sql,
        params,
    )

    columns = [
        description[0]

        for description
        in cursor.description
    ]

    rows = cursor.fetchmany(
        MAX_RESULT_ROWS
        + 1
    )

    truncated = (
        len(rows)
        > MAX_RESULT_ROWS
    )

    if truncated:

        rows = (
            rows[
                :MAX_RESULT_ROWS
            ]
        )

    total_rows = len(rows)
    if truncated:
        stats["sql_executions"] += 1
        total_rows = int(conn.execute(
            f"SELECT COUNT(*) FROM ({sql}) AS result_count", params,
        ).fetchone()[0])

    return (
        columns,
        rows,
        truncated,
        total_rows,
    )


# =============================================================================
# COMPILER LOOP
# =============================================================================

def compile_and_run(
    db_path: str,
    question: str,
) -> dict[str, Any]:

    reset_run_state()
    global RUN_DATABASE_NAME
    RUN_DATABASE_NAME = Path(db_path).stem

    database_path = (
        Path(
            db_path
        )
        .resolve()
    )

    uri = (
        f"file:"
        f"{database_path}"
        f"?mode=ro"
    )

    conn = sqlite3.connect(
        uri,
        uri=True,
    )

    conn.execute(
        "PRAGMA query_only = ON"
    )

    try:

        schema = (
            inspect_schema(
                conn
            )
        )
        profile_categories(conn, schema)

        rejected_bases: list[
            str
        ] = []

        base = (
            choose_base_table(
                question,
                schema,
                rejected_bases,
            )
        )

        program = QueryProgram(
            base_table=
                base
        )

        revisions = 0

        vet_details = None

        candidate_sql = None
        candidate_params: list[Any] = []
        force_next_filter = False
        vetted_candidates: set[str] = set()

        for _ in range(
            MAX_PLANNER_STEPS
        ):

            if force_next_filter:
                action = "ADD_FILTER"
                force_next_filter = False
                emit("decision", label="PLANNER", selected=action,
                     probability=1.0, confidence=1.0,
                     candidates=[{"label": action, "probability": 1.0}], automatic=True)
            else:
                action = choose_next_action(question, schema, program)

            if (
                action
                == "ADD_JOIN"
            ):

                choose_join(
                    question,
                    schema,
                    program,
                )

            elif (
                action
                == "ADD_OUTPUT"
            ):

                choose_output(
                    question,
                    schema,
                    program,
                )

            elif (
                action
                == "ADD_FILTER"
            ):

                choose_filter(
                    question,
                    schema,
                    program,
                    conn,
                )

            elif (
                action
                == "ADD_GROUP"
            ):

                choose_group(
                    question,
                    schema,
                    program,
                )

            elif action == "ADD_HAVING":
                choose_having(question, schema, program)

            elif (
                action
                == "SET_DISTINCT"
            ):

                choose_distinct(
                    question,
                    schema,
                    program,
                )

            elif (
                action
                == "SET_ORDER"
            ):

                choose_order(
                    question,
                    schema,
                    program,
                )

            elif (
                action
                == "SET_EXTREMUM"
            ):

                choose_extremum(
                    question,
                    schema,
                    program,
                )

            elif (
                action
                == "SET_LIMIT"
            ):

                choose_limit(
                    question,
                    schema,
                    program,
                )

            elif (
                action
                == "VET_QUERY"
            ):

                (
                    candidate_sql,
                    candidate_params,
                ) = compile_sql(
                    program
                )
                candidate_signature = stable_json([candidate_sql, candidate_params])
                if candidate_signature in vetted_candidates:
                    emit("status", message="The same candidate was already rejected; stopping revision")
                    break
                vetted_candidates.add(candidate_signature)
                emit("compiled", sql=candidate_sql, params=candidate_params, program=program.state())

                # Static code-level safety first.
                static_safety_check(
                    candidate_sql
                )

                # Then Jev sees the COMPLETE relational program
                # and final SQL before it is executed.
                (
                    passed,
                    verdict,
                    vet_details,
                ) = vet_query(
                    question,
                    schema,
                    program,
                    candidate_sql,
                )

                if passed:

                    # Semantic vet passed.
                    #
                    # Now, and only now, let SQLite plan the query.
                    explain_only(
                        conn,
                        candidate_sql,
                        candidate_params,
                    )

                    # Then execute the final SELECT.
                    (
                        columns,
                        rows,
                        truncated,
                        total_rows,
                    ) = execute_query(
                        conn,
                        candidate_sql,
                        candidate_params,
                    )

                    return {
                        "question":
                            question,

                        "schema":
                            schema,

                        "program":
                            program,

                        "sql":
                            candidate_sql,

                        "params":
                            candidate_params,

                        "columns":
                            columns,

                        "rows":
                            rows,

                        "truncated":
                            truncated,
                        "total_rows": total_rows,

                        "vet":
                            vet_details,

                        "unsupported":
                            False,
                    }

                revisions += 1

                if (
                    revisions
                    > MAX_REVISIONS
                ):

                    break

                before_revision = program.state()
                force_next_filter = verdict == "REVISE_FILTERS"
                if verdict == "REVISE_ORDER_OR_REDUCTION" and len(program.order_by) > 1:
                    revise_order_key(question, schema, program, vet_details)
                    revised = program
                else:
                    revised = apply_revision(program, verdict)

                if revised is None:

                    break

                if isinstance(revised, QueryProgram) and revised.state() == before_revision:
                    break

                if (
                    revised
                    == "RESELECT_SOURCE"
                ):

                    rejected_bases.append(
                        program.base_table
                    )

                    base = (
                        choose_base_table(
                            question,
                            schema,
                            rejected_bases,
                        )
                    )

                    program = QueryProgram(
                        base_table=
                            base
                    )

                else:

                    program = (
                        revised
                    )

            else:

                raise RuntimeError(
                    f"Unknown action: "
                    f"{action}"
                )

        # No final SQL is executed on failure.
        return {
            "question":
                question,

            "schema":
                schema,

            "program":
                program,

            "sql":
                candidate_sql,

            "params":
                candidate_params,

            "columns":
                [],

            "rows":
                [],

            "truncated":
                False,
            "total_rows": 0,

            "vet":
                vet_details,

            "unsupported":
                True,
        }

    finally:

        conn.close()


# =============================================================================
# DISPLAY
# =============================================================================

def print_schema(
    schema: SchemaInfo,
) -> None:

    print(
        "DATABASE SCHEMA"
    )

    print(
        "=" * 78
    )

    for table in (
        schema.tables.values()
    ):

        print(
            f"{table.name}("
        )

        for column in (
            table.columns
        ):

            flags = []

            if column.pk:

                flags.append(
                    "PK"
                )

            suffix = (
                f" [{' '.join(flags)}]"

                if flags

                else ""
            )

            print(
                f"  {column.name}: "
                f"{column.declared_type or column.kind}"
                f"{suffix}"
            )

        print(
            ")"
        )

    if (
        schema.foreign_keys
    ):

        print(
            "\nRelationships:"
        )

        for fk in (
            schema.foreign_keys
        ):

            print(
                " -",
                fk.label(),
            )


def format_table(
    columns: list[str],
    rows: list[
        tuple[Any, ...]
    ],
) -> str:

    if not columns:

        return (
            "(no columns)"
        )

    string_rows = [
        [
            (
                "NULL"

                if value is None

                else str(
                    value
                )
            )

            for value
            in row
        ]

        for row
        in rows
    ]

    widths = [
        len(
            column
        )

        for column
        in columns
    ]

    for row in string_rows:

        for (
            index,
            value,
        ) in enumerate(
            row
        ):

            widths[index] = max(
                widths[index],
                len(value),
            )

    header = (
        " | ".join(
            columns[index]
            .ljust(
                widths[index]
            )

            for index
            in range(
                len(
                    columns
                )
            )
        )
    )

    separator = (
        "-+-".join(
            "-"
            * width

            for width
            in widths
        )
    )

    body = [
        " | ".join(
            row[index]
            .ljust(
                widths[index]
            )

            for index
            in range(
                len(
                    columns
                )
            )
        )

        for row
        in string_rows
    ]

    return "\n".join(
        [
            header,
            separator,
            *body,
        ]
    )


def print_trace() -> None:

    print(
        "\nSELF-GENERATED CHOICE TRACE"
    )

    print(
        "=" * 78
    )

    for (
        index,
        item,
    ) in enumerate(
        TRACE,
        start=1,
    ):

        print(
            f"{index:02d}. "
            f"{item['kind']}: "
            f"{short(item['selected'], 180)} "
            f"P={item['probability']:.1%} "
            f"conf={item['confidence']:.1%}"
        )

        for (
            description,
            probability,
        ) in (
            item[
                "ranking"
            ][
                :4
            ]
        ):

            print(
                f"      "
                f"{probability:6.1%}  "
                f"{short(description, 150)}"
            )


def print_result(
    result: dict[str, Any],
) -> None:

    schema: SchemaInfo = (
        result[
            "schema"
        ]
    )

    program: QueryProgram = (
        result[
            "program"
        ]
    )

    print_schema(
        schema
    )

    print(
        "\nQUESTION"
    )

    print(
        "=" * 78
    )

    print(
        result[
            "question"
        ]
    )

    print_trace()

    print(
        "\nFINAL RELATIONAL PROGRAM"
    )

    print(
        "=" * 78
    )

    print(
        json.dumps(
            program.state(),
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )

    if (
        result[
            "unsupported"
        ]
    ):

        print(
            "\nFINAL ANSWER"
        )

        print(
            "=" * 78
        )

        print(
            "No query was executed because "
            "the compiler did not obtain a "
            "semantically vetted query within "
            "the revision budget."
        )

    else:

        print(
            "\nVETTED SQL"
        )

        print(
            "=" * 78
        )

        print(
            result[
                "sql"
            ]
        )

        if (
            result[
                "params"
            ]
        ):

            print(
                "params:",
                tuple(
                    result[
                        "params"
                    ]
                ),
            )

        print(
            "\nFINAL ANSWER"
        )

        print(
            "=" * 78
        )

        print(
            format_table(
                result[
                    "columns"
                ],
                result[
                    "rows"
                ],
            )
        )

        if (
            result[
                "truncated"
            ]
        ):

            print(
                f"\n[display truncated "
                f"after "
                f"{MAX_RESULT_ROWS} rows]"
            )

    print(
        "\nRUN COST"
    )

    print(
        "=" * 78
    )

    print(
        f"System One calls: "
        f"{stats['jev_calls']}"
    )

    print(
        f"Planner calls: "
        f"{stats['planner_calls']}"
    )

    print(
        f"Semantic vet calls: "
        f"{stats['vet_calls']}"
    )

    print(
        f"SQL executions: "
        f"{stats['sql_executions']}"
    )

    print(
        f"Input tokens: "
        f"{stats['input_tokens']:,}"
    )

    print(
        f"Output tokens: "
        f"{stats['output_tokens']:,}"
    )

    print(
        f"Input price: "
        f"${JEV_INPUT_USD_PER_MTOK:.3f} "
        f"/ 1M tokens"
    )

    print(
        f"Output price: "
        f"${JEV_OUTPUT_USD_PER_MTOK:.3f} "
        f"/ 1M tokens"
    )

    print(
        f"Estimated cost: "
        f"${total_cost_usd():.8f}"
    )

    print(
        f"Estimated cost in cents: "
        f"{total_cost_usd() * 100:.5f}"
    )


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Ask a SQLite database using Jev "
            "as a constrained semantic query compiler."
        )
    )

    parser.add_argument(
        "database",
        help=(
            "Path to a SQLite .db file"
        ),
    )

    parser.add_argument(
        "question",
        nargs="+",
        help=(
            "Natural-language database question"
        ),
    )

    args = (
        parser.parse_args()
    )

    database = Path(
        args.database
    )

    if not database.exists():

        raise SystemExit(
            f"Database not found: "
            f"{database}"
        )

    question = (
        " ".join(
            args.question
        )
        .strip()
    )

    result = (
        compile_and_run(
            str(
                database
            ),
            question,
        )
    )

    print_result(
        result
    )


if __name__ == "__main__":

    main()
