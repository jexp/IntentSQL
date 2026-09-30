"""Constrained SQLite mutation plans. Jev chooses; this module owns all SQL."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from intentsql.database import connect, readonly_uri
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import read_engine as engine
from .decision_context import DecisionClient, ReadState, Stage
from .jev_client import CallResult
from .semantic_read import Condition, compile_conditions, plan_conditions
from .skills.facts import extract_facts
from .skills.entity import Entity, explicitly_named_source
from .skills.schema import columns_for, numeric_profile
from .skills.range_value import column_uses_iso_dates
from .skills.value_hints import mechanically_related_values, targeted_values
from .request_routing import explicit_request_operation

MAX_AFFECTED = 100


@dataclass(frozen=True)
class RequestEvidence:
    value: Any
    start: int
    end: int
    kind: str

    @property
    def evidence_id(self) -> str:
        return f"{self.kind}:{self.start}:{self.end}"


@dataclass
class MutationProgram:
    operation: str
    table: str
    assignments: dict[str, Any] = field(default_factory=dict)
    where_column: str | None = None
    where_value: Any = None
    target_mode: str = "equality"
    stable_order_columns: tuple[str, ...] = ()
    value_provenance: list[dict[str, Any]] = field(default_factory=list)
    conditions: tuple[Condition, ...] = ()
    connector: str = "AND"


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    # Committed changes may still be in WAL rather than the main database file.
    for candidate in (path, Path(str(path) + "-wal")):
        if candidate.exists():
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def literals_from_request(question: str) -> list[RequestEvidence]:
    """Extract bounded, stable request spans without assigning their roles."""
    facts = extract_facts(question)
    found: "OrderedDict[str, RequestEvidence]" = OrderedDict()
    for fact in facts:
        if fact.kind in {"quoted_text", "integer", "number", "date", "worded_number"}:
            found[fact.fact_id] = RequestEvidence(fact.value, fact.start, fact.end, fact.kind)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9_-]*", question):
        evidence = RequestEvidence(match.group(), match.start(), match.end(), "word")
        found.setdefault(evidence.evidence_id, evidence)
    # Contiguous request spans are literal candidates, irrespective of case.
    # No dictionary supplies values. Roles and boundaries remain Jev choices.
    words = list(re.finditer(r"[A-Za-z][A-Za-z0-9_-]*", question))
    for width in range(2, 5):
        for index in range(len(words) - width + 1):
            group = words[index:index + width]
            if any(question[left.end():right.start()].strip()
                   for left, right in zip(group, group[1:])):
                continue
            start, end = group[0].start(), group[-1].end()
            if any(start < fact.end and end > fact.start for fact in facts):
                continue
            item = RequestEvidence(question[start:end], start, end, "text_span")
            found.setdefault(item.evidence_id, item)
    if len(found) > 240:
        raise ValueError("Too many literal candidates; shorten the change request or quote its values.")
    return list(found.values())


def _canonical_existing_value(conn: sqlite3.Connection, table: str, column: Any,
                              value: Any) -> tuple[bool, Any]:
    """Bind exact request evidence to one canonical stored value.

    SQLite text equality is case-sensitive by default. Natural-language case
    should not make an otherwise exact mutation target disappear, but a write
    must still fail closed if case-folding would identify multiple distinct
    stored values.
    """
    numeric = str(getattr(column, "declared_type", "")).upper()
    is_numeric = any(kind in numeric for kind in
                     ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))
    comparison = "= ?" if is_numeric or not isinstance(value, str) else "= ? COLLATE NOCASE"
    sql = (f"SELECT DISTINCT {engine.qident(column.name)} "
           f"FROM {engine.qident(table)} "
           f"WHERE {engine.qident(column.name)} {comparison} LIMIT 2")
    rows = conn.execute(sql, (value,)).fetchall()
    if len(rows) != 1:
        return False, None
    return True, rows[0][0]


def _mechanically_grounded_predicate_columns(conn: sqlite3.Connection, table: str,
                                              columns: tuple[Any, ...],
                                              request: str) -> tuple[str, ...]:
    """Find predicate columns backed by exact observed request values.

    Mutation assignment spans are masked before this runs, so an exact stored
    value still present in the request is evidence about OLD rows, even when
    that same column is also the UPDATE assignment target. Matching only
    establishes the column role; the shared predicate planner still chooses
    the operator and binds the canonical stored operand.
    """
    grounded: list[str] = []
    for column in columns:
        observed = targeted_values(conn, table, column.name, request)
        related = mechanically_related_values(
            request, tuple(str(value) for value in observed),
            column_name=column.name)
        if any(evidence.get("kind") == "exact_request_phrase"
               for _, evidence in related):
            grounded.append(column.name)
    return tuple(grounded)


def _has_grounded_mutation_value(conn: sqlite3.Connection, schema: engine.SchemaInfo,
                                  evidence: list[RequestEvidence]) -> bool:
    """Require a literal-like request value or an exact stored text value before Jev planning."""
    explicit_kinds = {"quoted_text", "integer", "number", "date", "worded_number"}
    if any(item.kind in explicit_kinds or
           (item.kind == "word" and re.search(r"\d", str(item.value)))
           for item in evidence):
        return True
    words = [item for item in evidence if item.kind in {"word", "text_span"}]
    for table in schema.tables.values():
        for column in table.columns:
            if column.kind != "text":
                continue
            for item in words:
                found, _ = _canonical_existing_value(
                    conn, table.name, column, item.value)
                if found:
                    return True
    return False


def state(question: str, schema: engine.SchemaInfo, stage: str,
          program: MutationProgram | None = None, **extra: Any) -> dict[str, Any]:
    established = ({"operation": program.operation, "table": program.table,
                    "assignments": program.assignments,
                    "conditions": [asdict(item) for item in program.conditions]}
                   if program else {})
    return {"request": question, "step": stage,
            "established": established, **extra}


def select(question: str, schema: engine.SchemaInfo, program: MutationProgram | None,
           stage: str, instruction: str, candidates: list[tuple[Any, Any]]) -> Any:
    selected, _ = engine.choice(state(question, schema, stage, program), instruction,
                                candidates, label=stage.upper())
    return selected


def assignment_columns(question: str, program: MutationProgram,
                       columns: list[Any]) -> tuple[str, ...]:
    """Judge independent assignment roles once, using only schema candidates."""
    candidates = columns
    if not candidates:
        return ()
    if program.operation == "UPDATE":
        set_clause = re.search(r"\bset\b(?P<body>.*)", question, re.I | re.S)
        if set_clause:
            body = re.split(r"\bwhere\b", set_clause.group("body"), maxsplit=1,
                            flags=re.I)[0]
            named = tuple(column.name for column in candidates if re.search(
                r"(?<!\w)" + re.escape(column.name).replace("_", r"[_\s]+") +
                r"\s*(?:to\b|=)", body, re.I))
            if named:
                engine.emit("skill", name="Assignment columns", selected=named,
                            source="explicit_set_clause_schema_match")
                return named
    questions = {
        f"c{index}": {"type": "noul",
                     "instructions": f"Does the request assign a NEW value to column {column.name}?",
                     "criteria": {"true": "This column receives a new stored value.",
                                  "false": "It only identifies rows or is not requested."}}
        for index, column in enumerate(candidates)
    }
    response = engine.system_one(
        {"request": question, "operation": program.operation,
         "table": program.table, "step": "assignment columns"}, questions)
    selected = []
    for index, column in enumerate(candidates):
        probability = float(response["answers"][f"c{index}"]["noul"])
        if probability >= .75:
            selected.append(column.name)
        elif probability > .25:
            raise ValueError(f"The assignment role of {column.name} is uncertain")
    return tuple(selected)


def bind_assignments(question: str, program: MutationProgram,
                     columns: list[Any], evidence: list[RequestEvidence],
                     conn: sqlite3.Connection | None = None) -> None:
    """Batch independent column-to-exact-span bindings; never regenerate values."""
    if not columns:
        raise ValueError("No requested assignment was grounded")
    questions, candidate_maps = {}, {}
    for index, column in enumerate(columns):
        numeric = any(kind in column.declared_type.upper()
                      for kind in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))
        date_semantics = (conn is not None and
                          column_uses_iso_dates(conn, program.table, column.name))
        if date_semantics:
            # SQLite NUMERIC affinity is often used for ISO date strings.
            # Use observed storage format, not declared affinity, to decide
            # which exact request spans are legal candidates.
            allowed_kinds = {"date", "quoted_text"}
            candidates = {item.evidence_id: item for item in evidence
                          if item.kind in allowed_kinds and
                          re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item.value))}
        elif numeric:
            candidates = {item.evidence_id: item for item in evidence
                          if item.kind in {"integer", "number", "worded_number"}}
        else:
            candidates = {item.evidence_id: item for item in evidence
                          if item.kind not in {"integer", "number", "worded_number"}}
        key = f"a{index}"
        candidate_maps[key] = (column.name, candidates)
        questions[key] = {"type": "choice",
            "instructions": f"Which exact request span is the NEW value assigned to {column.name}?",
            "criteria": {**{identifier: {"value": item.value,
                            "span": [item.start, item.end]}
                            for identifier, item in candidates.items()},
                         "none": "No new value for this column; only an old-row condition or no assignment"}}
    response = engine.system_one(
        {"request": question, "operation": program.operation, "table": program.table,
         "step": "bind new values; ignore old-row conditions"}, questions)
    for key, (name, candidates) in candidate_maps.items():
        answer = response["answers"][key]
        selected = answer.get("choice")
        if selected == "none":
            # Assignment-role judgments nominate columns. Binding establishes
            # assignments; final vetting independently detects missing changes.
            continue
        if selected not in candidates:
            raise ValueError(f"No exact request value was grounded for {name}")
        item = candidates[selected]
        program.assignments[name] = item.value
        program.value_provenance.append({"column": name, "evidence_id": selected,
            "start": item.start, "end": item.end, "value": item.value, "role": "assignment"})

    if not program.assignments:
        raise ValueError("No requested assignment was grounded")


def compile_mutation(program: MutationProgram, schema: engine.SchemaInfo) -> tuple[str, list[Any]]:
    if program.operation not in {"INSERT", "UPDATE", "DELETE"}:
        raise ValueError("Unsupported mutation operation")
    table = schema.tables.get(program.table)
    if table is None:
        raise ValueError("Unknown table")
    columns = {column.name: column for column in table.columns}
    if any(key not in columns for key in program.assignments):
        raise ValueError("Unknown assignment column")
    if program.operation in {"UPDATE", "DELETE"}:
        if program.target_mode == "first":
            if (not program.stable_order_columns or
                    any(name not in columns for name in program.stable_order_columns)):
                raise ValueError("A stable inspected ordering key is required")
        elif program.target_mode == "predicate":
            if not program.conditions:
                raise ValueError("A grounded row condition is required")
        elif program.target_mode == "equality":
            if not program.where_column or program.where_column not in columns:
                raise ValueError("An exact existing-column condition is required")
            if program.where_value is None:
                raise ValueError("A condition value is required")
        else:
            raise ValueError("Unsupported mutation target mode")
    if program.operation in {"INSERT", "UPDATE"} and not program.assignments:
        raise ValueError("At least one assignment is required")
    if program.operation == "UPDATE" and any(columns[key].pk for key in program.assignments):
        raise ValueError("Primary-key updates are disabled")
    if program.operation == "INSERT":
        names = list(program.assignments)
        sql = (f"INSERT INTO {engine.qident(program.table)} "
               f"({', '.join(map(engine.qident, names))}) "
               f"VALUES ({', '.join('?' for _ in names)})")
        return sql, [program.assignments[name] for name in names]
    params: list[Any]
    if program.target_mode == "first":
        keys = ", ".join(engine.qident(name) for name in program.stable_order_columns)
        outer = ("(" + keys + ")" if len(program.stable_order_columns) > 1 else keys)
        inner = ("SELECT " + keys + " FROM " + engine.qident(program.table) +
                 " ORDER BY " + ", ".join(engine.qident(name) + " ASC"
                                            for name in program.stable_order_columns) +
                 " LIMIT 1")
        predicate = f"{outer} = ({inner})"
        params = []
    elif program.target_mode == "predicate":
        predicate, params = compile_conditions(
            program.conditions, program.connector, tuple(columns))
        if not predicate:
            raise ValueError("A grounded row condition is required")
    else:
        predicate = f"{engine.qident(program.where_column)} = ?"
        params = [program.where_value]
    if program.operation == "DELETE":
        return f"DELETE FROM {engine.qident(program.table)} WHERE {predicate}", params
    assignments = ", ".join(f"{engine.qident(name)} = ?" for name in program.assignments)
    return (f"UPDATE {engine.qident(program.table)} SET {assignments} WHERE {predicate}",
            [*program.assignments.values(), *params])


def inspect_rows(conn: sqlite3.Connection, program: MutationProgram) -> list[dict[str, Any]]:
    if program.operation == "INSERT":
        return []
    if program.target_mode == "first":
        order = ", ".join(engine.qident(name) + " ASC"
                          for name in program.stable_order_columns)
        cursor = conn.execute(
            f"SELECT rowid AS _rowid_, * FROM {engine.qident(program.table)} "
            f"ORDER BY {order} LIMIT 1")
    elif program.target_mode == "predicate":
        predicate, params = compile_conditions(
            program.conditions, program.connector,
            tuple(column.name for column in columns_for(conn, program.table)))
        # The compiled predicate has already been schema-validated by
        # compile_mutation. Use its exact bound parameters for row preview.
        cursor = conn.execute(
            f"SELECT rowid AS _rowid_, * FROM {engine.qident(program.table)} "
            f"WHERE {predicate} LIMIT ?", (*params, MAX_AFFECTED + 1))
    else:
        cursor = conn.execute(
            f"SELECT rowid AS _rowid_, * FROM {engine.qident(program.table)} "
            f"WHERE {engine.qident(program.where_column)} = ? LIMIT ?",
            (program.where_value, MAX_AFFECTED + 1),
        )
    return [dict(row) for row in cursor.fetchall()]


def vet_mutation(question: str, schema: engine.SchemaInfo, program: MutationProgram,
                 sql: str, params: list[Any], before: list[dict[str, Any]],
                 preview: dict[str, Any]) -> dict[str, Any]:
    engine.stats["vet_calls"] += 1
    questions = {
        "intent": {"type": "noul", "instructions": "Does this exact operation and target table match the user's requested change?",
                   "criteria": {"true": "Operation and table match.", "false": "Wrong operation or table."}},
        "values": {"type": "noul", "instructions": "Do the assignments and grounded values match the request? A provenance entry may show that code bound request casing to one unique stored value.",
                   "criteria": {"true": "Values match directly or through the stated unique canonical binding.", "false": "A value is missing, wrong, or lacks grounding."}},
        "scope": {"type": "noul", "instructions": (
            "Does this INSERT create exactly the intended new record in the intended table?"
            if program.operation == "INSERT" else
            "Do the predicate and previewed existing rows target only the intended records?"),
                  "criteria": {"true": "Exactly the intended record(s).", "false": "Wrong, broad or uncertain scope."}},
        "verdict": {"type": "choice", "instructions": "PASS only if the entire program precisely implements the user request; otherwise reject.",
                    "criteria": {"PASS": "Safe and semantically exact.", "REJECT": "Incorrect, incomplete or uncertain."}},
    }
    response = engine.system_one(
        state(question, schema, "final semantic vet", program,
              sql=sql, bound_parameters=params,
              preview_affected_rows=preview["affected"],
              preview_before_rows=before[:10],
              preview_after_rows=preview["after"][:10],
              execution="No write occurs until user confirmation."),
        questions,
    )
    answers = response["answers"]
    checks = {name: float(answers[name]["noul"]) for name in ("intent", "values", "scope")}
    verdict = answers["verdict"]["choice"]
    passed = verdict == "PASS" and min(checks.values()) >= engine.VET_THRESHOLD
    engine.emit("vet", checks=checks, verdict=answers["verdict"], sql=sql, passed=passed)
    return {"passed": passed, "checks": checks, "verdict": verdict, "raw": answers}


def plan_mutation(db_path: Path, question: str, *, reset_state: bool = True,
                  operation_hint: str | None = None) -> dict[str, Any]:
    if reset_state:
        engine.reset_run_state()
    evidence = literals_from_request(question)
    uri = readonly_uri(db_path)
    with connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        schema = engine.inspect_schema(conn)
        surfaced = explicit_request_operation(question)
        mechanical_operation = (surfaced[1] if surfaced and surfaced[0] == "change" else None)
        operation = operation_hint or mechanical_operation
        if operation not in {"INSERT", "UPDATE", "DELETE"}:
            operation = select(question, schema, None, "choose operation",
                               "Which change operation did the user request?",
                               [(op, op) for op in ("INSERT", "UPDATE", "DELETE")])
        else:
            engine.emit("skill", name="Change operation", selected=operation,
                        source="explicit_request_operation")
        named_table = explicitly_named_source(question, tuple(schema.tables))
        if named_table:
            table_name = named_table
            engine.emit("skill", name="Mutation table", selected=table_name,
                        source="explicit_schema_name")
        else:
            table_name = select(question, schema, None, "choose table",
                                "Which table contains the records to change?",
                                [(name, {"table": name, "columns": [c.name for c in table.columns]})
                                 for name, table in schema.tables.items()])
        program = MutationProgram(operation=operation, table=table_name)
        table = schema.tables[table_name]
        if operation in ("INSERT", "UPDATE"):
            editable = [col for col in table.columns if not col.pk or operation == "INSERT"]
            selected_columns = assignment_columns(question, program, editable)
            bind_assignments(question, program,
                             [col for col in editable if col.name in selected_columns],
                             evidence, conn)
        if operation in ("UPDATE", "DELETE"):
            target_mode = select(
                question, schema, program, "choose target shape",
                "Is the target the first row under a stable key, or rows satisfying stated conditions?",
                [("predicate", "Rows satisfying the requested filter or comparison."),
                 ("first", "Exactly the first row under a stable database key.")])
            program.target_mode = target_mode
            if target_mode == "first":
                primary = tuple(column.name for column in table.columns if column.pk)
                if not primary:
                    raise ValueError("This table has no inspected primary key for deterministic first-row targeting.")
                program.stable_order_columns = primary
            elif target_mode == "predicate":
                # Mask only the selected assignment spans, preserving offsets/IDs.
                # An assigned column can independently constrain the old rows.
                chars = list(question)
                for binding in program.value_provenance:
                    if binding.get("role") == "assignment":
                        chars[binding["start"]:binding["end"]] = " " * (binding["end"] - binding["start"])
                predicate_request = "".join(chars)
                remaining_facts = extract_facts(predicate_request)

                class EngineDecisionClient:
                    def call(self, state: Any, questions: dict[str, Any]) -> CallResult:
                        response = engine.system_one(state, questions)
                        usage = response.get("usage") or {}
                        return CallResult(response, int(usage.get("input_tokens") or 0),
                                          int(usage.get("output_tokens") or 0), 0)

                client = DecisionClient(EngineDecisionClient(), ReadState(
                    table_name, operation=operation, assignments=tuple(program.assignments.items()), literal_facts=tuple(
                        (fact.fact_id, fact.kind, fact.value) for fact in remaining_facts)))
                client.advance(Stage.FILTER)

                def record_decision(name: str, selected: Any, result: Any) -> None:
                    engine.emit("skill", name=name, selected=selected,
                                source=getattr(result, "source", "jev_choice"),
                                usage=(result.trace[0] if result.trace else {}))

                predicate_columns = columns_for(conn, table_name)
                established_condition_columns = _mechanically_grounded_predicate_columns(
                    conn, table_name, predicate_columns, predicate_request)
                if established_condition_columns:
                    engine.emit("skill", name="Grounded condition columns",
                                selected=list(established_condition_columns),
                                source="exact_observed_request_evidence")
                conditions = plan_conditions(
                    conn, predicate_request, Entity(table_name, "resolved", "mutation_target"),
                    predicate_columns, client, remaining_facts,
                    record_decision, lambda t, c: numeric_profile(conn, t, c),
                    needs_filter=True,
                    established_columns=established_condition_columns)
                if not conditions.conditions:
                    raise ValueError("No source-row predicate was grounded; no change was previewed.")
                program.conditions = conditions.conditions
                program.connector = conditions.connector
            else:
                raise ValueError("The mutation target is unresolved")
        sql, params = compile_mutation(program, schema)
        engine.emit("compiled", sql=sql, params=params, program=asdict(program))
        before = inspect_rows(conn, program)
        if len(before) > MAX_AFFECTED:
            raise ValueError(f"More than {MAX_AFFECTED} rows match; narrow the request.")
        if operation in ("UPDATE", "DELETE") and not before:
            raise ValueError("No rows match the proposed condition; nothing will be changed.")
        try:
            preview = preview_mutation(conn, program, sql, params, before)
        except sqlite3.IntegrityError as exc:
            reason = (f"{len(before)} selected row(s); the database rejected the "
                      f"disposable preview: {exc}")
            engine.emit("preview_blocked", reason=reason, matched_rows=len(before))
            return {"supported": False, "program": asdict(program), "sql": sql,
                    "params": params, "before": before, "after": [],
                    "affected": len(before), "reason": reason,
                    "vet": {"passed": False, "verdict": "DATABASE_CONSTRAINT"}}
        vet = vet_mutation(question, schema, program, sql, params, before, preview)
        if not vet["passed"]:
            return {"supported": False, "program": asdict(program), "sql": sql,
                    "params": params, "before": before, "after": [], "vet": vet}
        return {"supported": True, "program": asdict(program), "sql": sql,
                "params": params, "before": before, "after": preview["after"],
                "affected": preview["affected"], "vet": vet, "fingerprint": fingerprint(db_path)}


def preview_mutation(source: sqlite3.Connection, program: MutationProgram,
                     sql: str, params: list[Any], before: list[dict[str, Any]]) -> dict[str, Any]:
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        source.backup(memory)
        memory.execute("PRAGMA foreign_keys=ON")
        memory.execute("BEGIN")
        cursor = memory.execute(sql, params)
        affected = cursor.rowcount
        if affected < 0 or affected > MAX_AFFECTED or memory.total_changes > MAX_AFFECTED:
            raise ValueError("Unsafe affected-row count")
        if program.operation == "INSERT":
            rows = memory.execute(f"SELECT rowid AS _rowid_, * FROM {engine.qident(program.table)} WHERE rowid=?",
                                  (cursor.lastrowid,)).fetchall()
        elif program.operation == "UPDATE":
            ids = [row["_rowid_"] for row in before]
            rows = memory.execute(f"SELECT rowid AS _rowid_, * FROM {engine.qident(program.table)} "
                                  f"WHERE rowid IN ({','.join('?' for _ in ids)})", ids).fetchall()
        else:
            rows = []
        memory.rollback()
        return {"affected": affected, "after": [dict(row) for row in rows]}
    finally:
        memory.close()


def commit_mutation(db_path: Path, plan: dict[str, Any]) -> int:
    if not plan.get("supported") or not plan.get("vet", {}).get("passed"):
        raise ValueError("Only vetted plans can be committed")
    if fingerprint(db_path) != plan["fingerprint"]:
        raise ValueError("Database changed since preview; run the request again")
    with connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        if fingerprint(db_path) != plan["fingerprint"]:
            raise ValueError("Database changed since preview; run the request again")
        cursor = conn.execute(plan["sql"], plan["params"])
        if (cursor.rowcount != plan["affected"] or cursor.rowcount > MAX_AFFECTED
                or conn.total_changes > MAX_AFFECTED):
            conn.rollback()
            raise ValueError("Affected rows changed since preview; rolled back")
        conn.commit()
        return cursor.rowcount
