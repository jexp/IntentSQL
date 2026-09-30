#!/usr/bin/env python3
"""Focused, repeatable benchmarks for independently developed Jev skills."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from intentsql.skills.intent import route_intent  # noqa: E402
from intentsql.skills.quantity import resolve_quantity  # noqa: E402
from intentsql.skills.entity import resolve_entity  # noqa: E402
from intentsql.skills.fields import resolve_fields  # noqa: E402
from intentsql.skills.schema import columns_for, direct_relations  # noqa: E402
from intentsql.skills.predicate import resolve_predicate_column, resolve_predicate_operator  # noqa: E402
from intentsql.skills.value_hints import small_category_hints, targeted_values  # noqa: E402
from intentsql.skills.predicate_value import resolve_predicate_value  # noqa: E402
from intentsql.skills.ordering import resolve_ordering  # noqa: E402
from intentsql.skills.predicate_columns import resolve_predicate_columns  # noqa: E402
from intentsql.skills.condition_logic import resolve_condition_logic  # noqa: E402
from intentsql.skills.aggregate import resolve_aggregate  # noqa: E402
from intentsql.skills.grouping import resolve_group_key  # noqa: E402
from intentsql.skills.group_ordering import resolve_group_ordering  # noqa: E402
from intentsql.skills.relationship_need import resolve_relationship_need  # noqa: E402
from intentsql.skills.filter_need import resolve_filter_need  # noqa: E402
from intentsql.skills.having import resolve_having  # noqa: E402
from intentsql.skills.relation import resolve_related_table  # noqa: E402
from intentsql.skills.join_fields import resolve_join_fields  # noqa: E402
from intentsql.skills.join_predicate import resolve_join_predicate_column  # noqa: E402
from intentsql.skills.rounding import resolve_rounding  # noqa: E402
from intentsql.skills.join_ordering import resolve_join_ordering  # noqa: E402
from intentsql.skills.stored_output import resolve_stored_output  # noqa: E402
from intentsql.skills.group_measure import resolve_group_measure  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["intent", "quantity", "entity", "fields", "predicate_column", "predicate_columns", "condition_logic", "operator", "predicate_value", "ordering", "aggregate", "grouping", "group_ordering", "group_measure", "relationship_need", "filter_need", "stored_output", "having", "relation", "join_fields", "join_predicate", "join_ordering", "rounding"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", help="Run cases whose request contains this text")
    parser.add_argument("--save", type=Path)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    fixture = json.loads((ROOT / "tools" / "fixtures" / f"{args.suite}_cases.json").read_text())
    results = []
    for case in fixture["cases"]:
        if args.only and args.only.lower() not in case["request"].lower():
            continue
        for iteration in range(args.repeat):
            if args.suite == "intent":
                result = route_intent(case["request"])
                value = {key: getattr(result, key) for key in case["expected"]}
                expected_status = "resolved"
            elif args.suite == "quantity":
                result = resolve_quantity(case["request"], case["mode"])
                value = result.value
                expected_status = case.get("status", "bounded")
            elif args.suite == "rounding":
                result = resolve_rounding(case["request"], case["measure"])
                value = result.places
                expected_status = "resolved" if case["expected"] is not None else "unsupported"
            elif args.suite == "entity":
                result = resolve_entity(case["request"], case["tables"])
                value = result.table
                expected_status = case.get("status", "resolved")
            elif args.suite == "fields":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_fields(case["request"], case["table"], columns)
                value = list(result.columns)
                expected_status = "resolved"
            elif args.suite == "predicate_column":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_predicate_column(case["request"], case["table"], columns)
                value = result.column
                expected_status = "resolved"
            elif args.suite == "predicate_columns":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_predicate_columns(case["request"], case["table"], columns)
                value = list(result.columns)
                expected_status = "resolved"
            elif args.suite == "condition_logic":
                result = resolve_condition_logic(case["request"], tuple(case["columns"]))
                value = result.connector
                expected_status = "resolved"
            elif args.suite == "aggregate":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_aggregate(case["request"], case["table"], columns)
                value = {"function": result.function, "column": result.column}
                expected_status = "resolved"
            elif args.suite == "grouping":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_group_key(case["request"], case["table"], columns)
                value = result.column
                expected_status = "resolved"
            elif args.suite == "group_ordering":
                result = resolve_group_ordering(case["request"], case["group_column"], case["measure"])
                value = {"target": result.target, "direction": result.direction,
                         "tie": result.tie_key_direction}
                expected_status = "resolved"
            elif args.suite == "relationship_need":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_relationship_need(case["request"], case["table"], columns)
                value = result.needs_other_table
                expected_status = "resolved"
            elif args.suite == "filter_need":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                    category_hints = (small_category_hints(conn, case["table"], columns)
                                      if case.get("group_measure_filter") else None)
                result = resolve_filter_need(case["request"], case["table"], columns,
                                             group_measure_filter=case.get("group_measure_filter", False),
                                             category_hints=category_hints)
                value = result.needed
                expected_status = "resolved"
            elif args.suite == "group_measure":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_group_measure(case["request"], case["table"],
                                               tuple(column.name for column in columns))
                value = {"kind": result.kind, "qualifies_groups": result.qualifies_groups}
                expected_status = "resolved"
            elif args.suite == "stored_output":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    table_columns = {table: columns_for(conn, table) for table in case["tables"]}
                result = resolve_stored_output(case["request"], table_columns)
                value = result.stored
                expected_status = "resolved"
            elif args.suite == "having":
                result = resolve_having(case["request"], case["group_key"], case["measure"])
                value = {"operator": result.operator, "value": result.value}
                expected_status = "resolved"
            elif args.suite == "relation":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    edges = direct_relations(conn, case["base_table"])
                    related_columns = {edge.other_table(case["base_table"]):
                        tuple(column.name for column in columns_for(conn, edge.other_table(case["base_table"])))
                        for edge in edges}
                result = resolve_related_table(case["request"], case["base_table"], edges,
                                               related_columns=related_columns)
                value = result.relation.other_table(case["base_table"]) if result.relation else None
                expected_status = "resolved"
            elif args.suite == "join_fields":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    table_columns = {table: columns_for(conn, table) for table in case["tables"]}
                result = resolve_join_fields(case["request"], table_columns)
                value = [f"{field.table}.{field.column}" for field in result.fields]
                expected_status = "resolved"
            elif args.suite == "join_predicate":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    table_columns = {table: columns_for(conn, table) for table in case["tables"]}
                result = resolve_join_predicate_column(case["request"], table_columns)
                value = f"{result.field.table}.{result.field.column}" if result.field else None
                expected_status = "resolved"
            elif args.suite == "join_ordering":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    table_columns = {table: columns_for(conn, table) for table in case["tables"]}
                result = resolve_join_ordering(case["request"], table_columns)
                value = {"field": f"{result.field.table}.{result.field.column}" if result.field else None,
                         "direction": result.direction}
                expected_status = "resolved"
            elif args.suite == "ordering":
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                result = resolve_ordering(case["request"], case["table"], columns)
                value = {"column": result.column, "direction": result.direction}
                expected_status = "resolved"
            elif args.suite in ("operator", "predicate_value"):
                path = ROOT / "data" / f"{case['database']}.db"
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
                    columns = columns_for(conn, case["table"])
                    hints = targeted_values(conn, case["table"], case["column"], case["request"])
                column = next(column for column in columns if column.name == case["column"])
                if args.suite == "operator":
                    result = resolve_predicate_operator(case["request"], column, value_hints=hints)
                    value = result.operator
                else:
                    result = resolve_predicate_value(case["request"], column,
                                                     case["operator"], hints)
                    value = result.value
                expected_status = "resolved"
            if args.suite == "intent":
                passed = value == case["expected"]
                status, source, calls = "resolved", "jev_route", 1
            else:
                if args.suite == "ordering":
                    expected = {"column": case["column"], "direction": case["direction"]}
                else:
                    expected = case["expected"]
                passed = value == expected and result.status == expected_status
                status, source, calls = result.status, result.source, result.jev_calls
            row = {"request": case["request"], "iteration": iteration + 1,
                   "expected": case.get("expected", {"column": case.get("column"),
                                                     "direction": case.get("direction")}),
                   "passed": passed,
                   "jev_calls": calls, **asdict(result)}
            results.append(row)
            print(f"{'PASS' if passed else 'FAIL'} {case['request']!r} -> {value} "
                  f"[{status}/{source}] calls={calls} "
                  f"tokens={result.input_tokens}+{result.output_tokens}")
    totals = {"passed": sum(row["passed"] for row in results), "cases": len(results),
              "jev_calls": sum(row["jev_calls"] for row in results),
              "input_tokens": sum(row["input_tokens"] for row in results),
              "output_tokens": sum(row["output_tokens"] for row in results)}
    print(json.dumps(totals, sort_keys=True))
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                                        "suite": args.suite, "repeat": args.repeat,
                                        "totals": totals, "results": results}, indent=2), encoding="utf-8")
    if totals["passed"] != totals["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
