"""Independent reference pack for the bundled databases.

Reference rows come from SQLite, not from IntentSQL. A case passes only when
those rows agree and the typed plan has the semantic shape named below.
This file is not part of unittest discovery.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.v1.run_benchmark import is_infrastructure_error, rows_equivalent  # noqa: E402
from intentsql.database import connect  # noqa: E402
from intentsql.jev_client import JevClient  # noqa: E402
from intentsql.semantic_read import run_read  # noqa: E402
from intentsql import mutations  # noqa: E402


DATA = ROOT / "data"

# Each case names one semantic slot. Adversarial pairs share a pair id and
# must not collapse to the same plan.
CASES: tuple[dict, ...] = (
    {"id": "CYB-EQ", "db": "cyberchase.db", "pair": "code",
     "prompt": "Show the title of the episode whose production code is CYB001.",
     "reference_sql": "SELECT title FROM episodes WHERE production_code = 'CYB001'",
     "shape": {"tables": ["episodes"], "no_join": True,
               "filters": [{"column": "production_code", "operator": "=", "value": "CYB001"}],
               "aggregate": None, "groups": []}},
    {"id": "CYB-FIELDS", "db": "cyberchase.db", "pair": "season-output",
     "prompt": "Show the title of every episode in season 2.",
     "reference_sql": "SELECT title FROM episodes WHERE season = 2",
     "shape": {"tables": ["episodes"], "no_join": True,
               "filters": [{"column": "season", "operator": "=", "value": 2}],
               "outputs_include": ["title"], "aggregate": None, "groups": [],
               "not_whole_row": True}},
    {"id": "CYB-ROWS", "db": "cyberchase.db", "pair": "season-output",
     "prompt": "Show every column of every episode in season 2.",
     "reference_sql": "SELECT * FROM episodes WHERE season = 2",
     "shape": {"tables": ["episodes"], "no_join": True,
               "filters": [{"column": "season", "operator": "=", "value": 2}],
               "whole_row": True, "aggregate": None, "groups": []}},
    {"id": "CYB-BETWEEN", "db": "cyberchase.db", "pair": "air-window",
     "prompt": "Show the titles of episodes that aired from 2002-01-21 through 2002-01-23, earliest air date first.",
     "reference_sql": "SELECT title FROM episodes WHERE air_date BETWEEN '2002-01-21' AND '2002-01-23' ORDER BY air_date",
     "ordered": True,
     "shape": {"tables": ["episodes"],
               "filters": [{"column": "air_date", "operator": "BETWEEN",
                            "value": ["2002-01-21", "2002-01-23"]}],
               "order": [{"column": "air_date", "direction": "ASC"}],
               "aggregate": None}},
    {"id": "CYB-STRICT", "db": "cyberchase.db", "pair": "air-window",
     "prompt": "Show the titles of episodes that aired after 2002-01-21 and before 2002-01-23.",
     "reference_sql": "SELECT title FROM episodes WHERE air_date > '2002-01-21' AND air_date < '2002-01-23'",
     "shape": {"tables": ["episodes"], "connector": "AND",
               "filters": [{"column": "air_date", "operator": ">", "value": "2002-01-21"},
                           {"column": "air_date", "operator": "<", "value": "2002-01-23"}],
               "forbid_operators": ["BETWEEN"], "aggregate": None}},
    {"id": "CYB-COUNT", "db": "cyberchase.db", "pair": "season-count",
     "prompt": "How many episodes are in season 1?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM episodes WHERE season = 1",
     "scalar": True,
     "shape": {"filters": [{"column": "season", "operator": "=", "value": 1}],
               "aggregate": "COUNT", "groups": [], "limit": None}},
    {"id": "CYB-GROUP", "db": "cyberchase.db", "pair": "season-count",
     "prompt": "How many episodes are in each season?",
     "reference_sql": "SELECT season, COUNT(*) AS row_count FROM episodes GROUP BY season",
     "shape": {"aggregate": "COUNT", "groups": ["season"],
               "no_filter_columns": ["season"], "limit": None}},
    {"id": "CYB-ONE", "db": "cyberchase.db", "pair": "earliest",
     "prompt": "Show the title of the earliest aired episode.",
     "reference_sql": "SELECT title FROM episodes ORDER BY air_date ASC LIMIT 1",
     "shape": {"one_of": [
         {"order": [{"column": "air_date", "direction": "ASC"}], "limit": 1, "aggregate": None},
         {"extremum": {"column": "air_date", "function": "MIN"}, "limit": None, "aggregate": None}]}},
    {"id": "CYB-FIVE", "db": "cyberchase.db", "pair": "earliest",
     "prompt": "Show the titles of the five earliest aired episodes, earliest first.",
     "reference_sql": "SELECT title FROM episodes ORDER BY air_date ASC LIMIT 5",
     "ordered": True,
     "shape": {"order": [{"column": "air_date", "direction": "ASC"}], "limit": 5,
               "aggregate": None, "no_extremum": True}},
    {"id": "CYB-NO-TOPIC", "db": "cyberchase.db", "pair": "topic-null",
     "prompt": "How many episodes have no topic?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM episodes WHERE topic IS NULL",
     "scalar": True,
     "shape": {"filters": [{"column": "topic", "operator": "IS NULL"}],
               "aggregate": "COUNT"}},
    {"id": "CYB-HAS-TOPIC", "db": "cyberchase.db", "pair": "topic-null",
     "prompt": "How many episodes have a topic?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM episodes WHERE topic IS NOT NULL",
     "scalar": True,
     "shape": {"filters": [{"column": "topic", "operator": "IS NOT NULL"}],
               "aggregate": "COUNT"}},
    {"id": "CYB-OR", "db": "cyberchase.db",
     "prompt": "How many episodes are in season 1 or season 2?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM episodes WHERE season IN (1, 2)",
     "scalar": True,
     "shape": {"aggregate": "COUNT", "value_set": {"column": "season", "values": [1, 2]}}},
    {"id": "CYB-AND", "db": "cyberchase.db",
     "prompt": "Show the title of season 1 episodes whose topic is Navigation.",
     "reference_sql": "SELECT title FROM episodes WHERE season = 1 AND topic = 'Navigation'",
     "shape": {"connector": "AND", "aggregate": None,
               "filters": [{"column": "season", "operator": "=", "value": 1},
                           {"column": "topic", "operator": "=", "value": "Navigation"}]}},
    {"id": "CYB-DISTINCT", "db": "cyberchase.db",
     "prompt": "List the distinct topic values stored for episodes.",
     "reference_sql": "SELECT DISTINCT topic FROM episodes",
     "shape": {"distinct": True, "outputs_include": ["topic"], "aggregate": None,
               "groups": [], "filters": []}},
    {"id": "CYB-TIES", "db": "cyberchase.db",
     "prompt": "For each season, show the season and title of every episode that has that season's earliest air date.",
     "reference_sql": """
        SELECT season, title FROM episodes AS episode
        WHERE air_date = (SELECT MIN(air_date) FROM episodes AS sibling
                          WHERE sibling.season = episode.season)
     """,
     "shape": {"per_group": {"group_column": "season", "extremum_column": "air_date",
                             "function": "MIN"},
               "outputs_include": ["season", "title"], "limit": None}},
    {"id": "CYB-VAGUE", "db": "cyberchase.db", "expect": "reject",
     "prompt": "Show the good episodes."},
    {"id": "CYB-NESTED", "db": "cyberchase.db", "expect": "reject",
     "prompt": "Show episodes that are in season 1 and about Navigation, or in season 2 and about Measurement."},
    {"id": "CYB-NO-REL", "db": "cyberchase.db", "expect": "reject",
     "prompt": "Show episodes together with the schools they belong to."},

    {"id": "DESE-CITIES", "db": "dese.db", "pair": "public-group",
     "prompt": "How many schools are in each city?",
     "reference_sql": "SELECT city, COUNT(*) AS row_count FROM schools GROUP BY city",
     "shape": {"aggregate": "COUNT", "groups": ["city"], "no_filter_columns": ["type"]}},
    {"id": "DESE-PUBLIC", "db": "dese.db", "pair": "public-group",
     "prompt": "How many public schools are in each city?",
     "reference_sql": "SELECT city, COUNT(*) AS row_count FROM schools WHERE type = 'Public School' GROUP BY city",
     "shape": {"aggregate": "COUNT", "groups": ["city"],
               "filters": [{"column": "type", "operators": ["=", "PREFIX", "CONTAINS"],
                            "value_contains": "public"}]}},
    {"id": "DESE-CITY", "db": "dese.db", "pair": "springfield",
     "prompt": "Show the name of every school in Springfield.",
     "reference_sql": "SELECT name FROM schools WHERE city = 'Springfield'",
     "shape": {"filters": [{"column": "city", "operator": "=", "value": "Springfield"}],
               "no_filter_columns": ["type"], "outputs_include": ["name"], "aggregate": None}},
    {"id": "DESE-PUBLIC-CITY", "db": "dese.db", "pair": "springfield",
     "prompt": "Show the name of every public school in Springfield.",
     "reference_sql": "SELECT name FROM schools WHERE city = 'Springfield' AND type = 'Public School'",
     "shape": {"connector": "AND", "aggregate": None, "outputs_include": ["name"],
               "filters": [{"column": "city", "operator": "=", "value": "Springfield"},
                           {"column": "type", "operators": ["=", "PREFIX", "CONTAINS"],
                            "value_contains": "public"}]}},
    {"id": "DESE-GRAD", "db": "dese.db",
     "prompt": "Show the school name for every school whose graduated value is 100.",
     "reference_sql": """
        SELECT schools.name FROM schools
        JOIN graduation_rates ON schools.id = graduation_rates.school_id
        WHERE graduation_rates.graduated = 100
     """,
     "shape": {"tables": ["schools", "graduation_rates"],
               "filters": [{"column": "graduated", "operator": "=", "value": 100}],
               "no_filter_columns": ["dropped", "excluded"],
               "outputs_include": ["name"], "aggregate": None, "groups": []}},
    {"id": "DESE-PUPILS", "db": "dese.db", "pair": "pupils",
     "prompt": "Show the district name and pupil count for districts in Boston.",
     "expect": "rows_or_reject",
     "reference_sql": """
        SELECT districts.name, expenditures.pupils
        FROM districts JOIN expenditures ON districts.id = expenditures.district_id
        WHERE districts.city = 'Boston'
     """,
     "shape": {"tables": ["districts", "expenditures"], "aggregate": None, "groups": [],
               "filters": [{"column": "city", "operator": "=", "value": "Boston"}],
               "outputs_include": ["name", "pupils"]}},
    {"id": "DESE-EXP-COUNT", "db": "dese.db", "pair": "pupils", "expect": "reject",
     "prompt": "How many expenditure records belong to districts in Boston?"},
    {"id": "DESE-TWO", "db": "dese.db",
     "prompt": "How many schools of each type are in each city?",
     "reference_sql": "SELECT city, type, COUNT(*) AS row_count FROM schools GROUP BY city, type",
     "shape": {"aggregate": "COUNT", "groups": ["city", "type"], "no_filter_columns": ["type", "city"]}},

    {"id": "MB-CANADA", "db": "moneyball.db", "pair": "birth-country",
     "prompt": "How many players were born in Canada?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM players WHERE birth_country = 'CAN'",
     "scalar": True,
     "shape": {"aggregate": "COUNT",
               "filters": [{"column": "birth_country", "operator": "=", "value": "CAN"}]}},
    {"id": "MB-CAN", "db": "moneyball.db", "pair": "birth-country",
     "prompt": "How many players have birth country code CAN?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM players WHERE birth_country = 'CAN'",
     "scalar": True,
     "shape": {"aggregate": "COUNT",
               "filters": [{"column": "birth_country", "operator": "=", "value": "CAN"}]}},
    {"id": "MB-MAX", "db": "moneyball.db", "pair": "salary-top",
     "prompt": "What is the highest salary?",
     "reference_sql": "SELECT MAX(salary) AS salary FROM salaries",
     "scalar": True,
     "shape": {"one_of": [
         {"aggregate": "MAX", "aggregate_column": "salary", "limit": None, "groups": []},
         {"order": [{"column": "salary", "direction": "DESC"}], "limit": 1,
          "aggregate": None, "groups": []}]}},
    {"id": "MB-FIVE", "db": "moneyball.db", "pair": "salary-top",
     "prompt": "Show the five highest salaries, largest first.",
     "reference_sql": "SELECT salary FROM salaries ORDER BY salary DESC LIMIT 5",
     "ordered": True, "scalar": False,
     "shape": {"order": [{"column": "salary", "direction": "DESC"}], "limit": 5,
               "aggregate": None, "groups": [], "no_extremum": True}},
    {"id": "MB-RANGE", "db": "moneyball.db", "pair": "height",
     "prompt": "How many players have a height between 80 and 82?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM players WHERE height BETWEEN 80 AND 82",
     "scalar": True,
     "shape": {"aggregate": "COUNT",
               "filters": [{"column": "height", "operator": "BETWEEN", "value": [80, 82]}]}},
    {"id": "MB-GT", "db": "moneyball.db", "pair": "height",
     "prompt": "How many players are taller than 80 inches?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM players WHERE height > 80",
     "scalar": True,
     "shape": {"aggregate": "COUNT",
               "filters": [{"column": "height", "operator": ">", "value": 80}],
               "forbid_operators": ["BETWEEN"]}},
    {"id": "MB-DEBUT", "db": "moneyball.db",
     "prompt": "How many players have no debut date?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM players WHERE debut IS NULL",
     "scalar": True,
     "shape": {"aggregate": "COUNT", "filters": [{"column": "debut", "operator": "IS NULL"}]}},
    {"id": "MB-BATS", "db": "moneyball.db",
     "prompt": "How many players bat L or R?",
     "reference_sql": "SELECT COUNT(*) AS row_count FROM players WHERE bats IN ('L', 'R')",
     "scalar": True,
     "shape": {"aggregate": "COUNT", "value_set": {"column": "bats", "values": ["L", "R"]}}},
    {"id": "MB-ORDER", "db": "moneyball.db",
     "prompt": "Show the last name and first name of players born in 1980, ordered by last name then first name.",
     "reference_sql": """
        SELECT last_name, first_name FROM players
        WHERE birth_year = 1980 ORDER BY last_name ASC, first_name ASC
     """,
     "ordered": True,
     "shape": {"filters": [{"column": "birth_year", "operator": "=", "value": 1980}],
               "order": [{"column": "last_name", "direction": "ASC"},
                         {"column": "first_name", "direction": "ASC"}],
               "outputs_include": ["last_name", "first_name"], "aggregate": None}},
    {"id": "MB-HAVING", "db": "moneyball.db",
     "prompt": "From 1998 through 2000, show the year and maximum home runs for years whose maximum home runs was at least 65. Highest maximum first.",
     "reference_sql": """
        SELECT year, MAX(HR) AS max_HR FROM performances
        WHERE year BETWEEN 1998 AND 2000
        GROUP BY year HAVING MAX(HR) >= 65
        ORDER BY MAX(HR) DESC, year ASC
     """,
     "ordered": True,
     "shape": {"filters": [{"column": "year", "operator": "BETWEEN", "value": [1998, 2000]}],
               "groups": ["year"], "aggregate": "MAX", "aggregate_column": "HR",
               "having": {"operator": ">=", "value": 65},
               "order": [{"column": "max_hr", "direction": "DESC"}]}},
    {"id": "MB-AVG", "db": "moneyball.db",
     "prompt": "What is the average height of players born in Canada?",
     "reference_sql": "SELECT AVG(height) AS avg_height FROM players WHERE birth_country = 'CAN'",
     "scalar": True,
     "shape": {"aggregate": "AVG", "aggregate_column": "height", "groups": [],
               "filters": [{"column": "birth_country", "operator": "=", "value": "CAN"}]}},
    {"id": "MB-SUM", "db": "moneyball.db",
     "prompt": "What is the total of the salary values recorded for 2001?",
     "reference_sql": "SELECT SUM(salary) AS sum_salary FROM salaries WHERE year = 2001",
     "scalar": True,
     "shape": {"aggregate": "SUM", "aggregate_column": "salary", "groups": [],
               "filters": [{"column": "year", "operator": "=", "value": 2001}]}},
    {"id": "MB-MIN", "db": "dese.db",
     "prompt": "What is the smallest pupil count?",
     "reference_sql": "SELECT MIN(pupils) AS min_pupils FROM expenditures",
     "scalar": True,
     "shape": {"aggregate": "MIN", "aggregate_column": "pupils", "groups": [], "filters": []}},

    {"id": "MUT-INSERT", "db": "cyberchase.db", "expect": "mutation",
     "prompt": "Insert an episode titled Pack Probe into the episodes table.",
     "mutation": {"operation": "INSERT", "table": "episodes", "affected": 1,
                  "assignments": {"title": "Pack Probe"}}},
    {"id": "MUT-UPDATE", "db": "cyberchase.db", "expect": "mutation",
     "prompt": "Set the topic to Navigation for the episode whose production code is CYB010.",
     "mutation": {"operation": "UPDATE", "table": "episodes", "affected": 1,
                  "assignments": {"topic": "Navigation"},
                  "filters": [{"column": "production_code", "operator": "=", "value": "CYB010"}]}},
    {"id": "MUT-ALL", "db": "moneyball.db", "expect": "reject",
     "prompt": "Delete every row in the players table."},
    {"id": "MUT-DDL", "db": "moneyball.db", "expect": "reject",
     "prompt": "Drop the players table."},
)


def _filters(program: dict) -> list[dict]:
    return [item for item in (program.get("filters") or []) if isinstance(item, dict)]


def _same(actual, expected) -> bool:
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        return [ _cell(item) for item in actual ] == [ _cell(item) for item in expected ]
    return _cell(actual) == _cell(expected)


def _cell(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _filter_matches(got: dict, wanted: dict) -> bool:
    if got.get("column") != wanted["column"]:
        return False
    if "operator" in wanted and got.get("operator") != wanted["operator"]:
        return False
    if "operators" in wanted and got.get("operator") not in wanted["operators"]:
        return False
    if "value" in wanted and not _same(got.get("value"), wanted["value"]):
        return False
    if "value_contains" in wanted:
        if wanted["value_contains"].casefold() not in str(got.get("value")).casefold():
            return False
    return True


def _order_matches(program: dict, wanted: list[dict]) -> bool:
    actual = program.get("order_by") or []
    if len(actual) < len(wanted):
        return False
    for got, item in zip(actual, wanted):
        key = str(got.get("key") or "").casefold().replace('"', "")
        if item["column"].casefold() not in key:
            return False
        if got.get("direction") != item["direction"]:
            return False
    return True


def _groups(program: dict) -> list[str]:
    found = []
    for item in program.get("groups") or []:
        if isinstance(item, dict):
            found.append(item.get("group by") or item.get("column"))
        else:
            found.append(item)
    return [str(item) for item in found if item]


def _aggregate(program: dict) -> tuple[str | None, str | None]:
    typed = program.get("typed_query") or {}
    function = typed.get("aggregate_function")
    column = typed.get("aggregate_column")
    if typed.get("output") == "count":
        return "COUNT", column
    return function, column


def _shape_holds(program: dict, sql: str, spec: dict) -> list[str]:
    reasons: list[str] = []
    filters = _filters(program)
    function, column = _aggregate(program)
    if "tables" in spec:
        present = {str(item).casefold() for item in (program.get("joined_tables") or [])}
        present.add(str(program.get("base_table") or "").casefold())
        for table in spec["tables"]:
            if table.casefold() not in present and f'"{table}"' not in sql:
                reasons.append(f"missing table {table}")
    if spec.get("no_join") and program.get("joins"):
        reasons.append(f"unexpected join {program.get('joins')}")
    if "connector" in spec and program.get("filter_connector") != spec["connector"]:
        reasons.append(f"connector {program.get('filter_connector')!r}")
    for wanted in spec.get("filters") or []:
        if not any(_filter_matches(got, wanted) for got in filters):
            reasons.append(f"missing filter {wanted}")
    for name in spec.get("no_filter_columns") or []:
        if any(got.get("column") == name for got in filters):
            reasons.append(f"unexpected filter on {name}")
    if spec.get("filters") == [] and filters:
        reasons.append(f"unexpected filters {filters}")
    for operator in spec.get("forbid_operators") or []:
        if any(got.get("operator") == operator for got in filters):
            reasons.append(f"forbidden operator {operator}")
    if "value_set" in spec:
        slot = spec["value_set"]
        values = []
        connector = program.get("filter_connector")
        for got in filters:
            if got.get("column") != slot["column"]:
                continue
            if got.get("operator") == "IN":
                values.extend(got.get("value") or [])
            elif got.get("operator") == "=":
                values.append(got.get("value"))
        uses_in = any(got.get("operator") == "IN" and got.get("column") == slot["column"]
                      for got in filters)
        if { _cell(item) for item in values } != { _cell(item) for item in slot["values"] }:
            reasons.append(f"value set {values!r} != {slot['values']!r}")
        elif not uses_in and connector != "OR":
            reasons.append("alternative values are not one IN predicate or an OR of equalities")
    if "groups" in spec and sorted(_groups(program)) != sorted(spec["groups"]):
        reasons.append(f"groups {_groups(program)!r} != {spec['groups']!r}")
    if "aggregate" in spec and function != spec["aggregate"]:
        reasons.append(f"aggregate {function!r} != {spec['aggregate']!r}")
    if "aggregate_column" in spec and column != spec["aggregate_column"]:
        reasons.append(f"aggregate column {column!r} != {spec['aggregate_column']!r}")
    if "having" in spec:
        having = program.get("having") or []
        wanted = spec["having"]
        if not any(item.get("operator") == wanted["operator"] and _same(item.get("value"), wanted["value"])
                   for item in having if isinstance(item, dict)):
            reasons.append(f"missing having {wanted} in {having}")
    if "order" in spec and not _order_matches(program, spec["order"]):
        reasons.append(f"order {program.get('order_by')!r}")
    if "limit" in spec and program.get("limit") != spec["limit"]:
        reasons.append(f"limit {program.get('limit')!r} != {spec['limit']!r}")
    if "distinct" in spec:
        distinct = program.get("distinct")
        actual = distinct is True
        if actual != spec["distinct"]:
            reasons.append(f"distinct {distinct!r}")
    outputs = [str(item).casefold() for item in (program.get("outputs") or [])]
    for name in spec.get("outputs_include") or []:
        key = name.casefold()
        if not any(item == key or item.endswith("." + key) or item.endswith("_" + key) for item in outputs):
            reasons.append(f"missing output {name}")
    if spec.get("whole_row") and "all columns" not in outputs:
        reasons.append(f"expected a whole-row read, got {outputs}")
    if spec.get("not_whole_row") and "all columns" in outputs:
        reasons.append("expected selected fields, got every column")
    extremum = program.get("per_group_extremum") or program.get("global_extremum")
    if spec.get("no_extremum") and extremum:
        reasons.append(f"unexpected extremum {extremum}")
    if "extremum" in spec:
        got = program.get("global_extremum") or {}
        for key, value in spec["extremum"].items():
            if got.get(key) != value:
                reasons.append(f"extremum {key} {got.get(key)!r} != {value!r}")
    if "per_group" in spec:
        got = program.get("per_group_extremum") or {}
        for key, value in spec["per_group"].items():
            if got.get(key) != value:
                reasons.append(f"per-group {key} {got.get(key)!r} != {value!r}")
    return reasons


def _shape_ok(program: dict, sql: str, spec: dict) -> list[str]:
    if "one_of" not in spec:
        return _shape_holds(program, sql, spec)
    branches = []
    for option in spec["one_of"]:
        merged = {key: value for key, value in spec.items() if key != "one_of"}
        merged.update(option)
        branches.append(_shape_holds(program, sql, merged))
    if any(not reasons for reasons in branches):
        return []
    return ["no accepted shape: " + " || ".join("; ".join(reasons) for reasons in branches)]


def _project(actual_columns, actual_rows, gold_columns, gold_rows, ordered: bool):
    mapping = []
    for gold in gold_columns:
        key = gold.casefold()
        found = None
        for index, name in enumerate(actual_columns):
            label = str(name).casefold()
            if label == key or label.endswith("_" + key):
                found = index
                break
        if found is None:
            return False, f"result columns {actual_columns} do not include {gold}"
        mapping.append(found)
    projected = [[_cell(row[index]) for index in mapping] for row in actual_rows]
    gold = [[_cell(value) for value in row] for row in gold_rows]
    if rows_equivalent(projected, gold, ordered):
        return True, ""
    return False, f"rows differ: got {projected[:8]} reference {gold[:8]}"


def _reference(connection: sqlite3.Connection, sql: str):
    cursor = connection.execute(sql)
    columns = [item[0] for item in cursor.description]
    return columns, cursor.fetchall()


def _mutation_reasons(plan: dict, spec: dict) -> list[str]:
    reasons = []
    program = plan.get("program") or {}
    if plan.get("supported") is not True:
        return [f"mutation was not previewed: {plan.get('reason') or plan.get('vet')}"]
    if program.get("operation") != spec["operation"]:
        reasons.append(f"operation {program.get('operation')!r}")
    if program.get("table") != spec["table"]:
        reasons.append(f"table {program.get('table')!r}")
    if plan.get("affected") != spec["affected"]:
        reasons.append(f"affected {plan.get('affected')!r} != {spec['affected']}")
    assignments = program.get("assignments") or {}
    for column, value in spec.get("assignments", {}).items():
        if assignments.get(column) != value:
            reasons.append(f"assignment {column}={assignments.get(column)!r} != {value!r}")
    filters = program.get("conditions") or []
    for wanted in spec.get("filters") or []:
        if not any(_filter_matches(got, wanted) for got in filters):
            reasons.append(f"missing mutation filter {wanted}")
    return reasons


def run_case(case: dict) -> dict:
    path = DATA / case["db"]
    started = time.perf_counter()
    record = {"id": case["id"], "db": case["db"], "pair": case.get("pair"),
              "prompt": case["prompt"], "expect": case.get("expect", "rows"),
              "passed": False, "infrastructure": False, "reasons": []}
    try:
        if case.get("expect") == "mutation" or case["db"] == "moneyball.db" and case.get("expect") == "reject" and case["id"].startswith("MUT"):
            with tempfile.TemporaryDirectory() as folder:
                copy = Path(folder) / case["db"]
                shutil.copy(path, copy)
                before = copy.read_bytes()
                try:
                    plan = mutations.plan_mutation(copy, case["prompt"])
                except Exception as exc:
                    if is_infrastructure_error(str(exc)):
                        raise
                    plan = {"supported": False, "reason": str(exc)}
                if copy.read_bytes() != before:
                    record["reasons"].append("preview wrote the database copy")
                if case.get("expect") == "reject":
                    record["passed"] = plan.get("supported") is not True and not record["reasons"]
                    if not record["passed"] and not record["reasons"]:
                        record["reasons"].append("mutation was previewed as supported")
                    record["sql"] = plan.get("sql")
                else:
                    record["reasons"].extend(_mutation_reasons(plan, case["mutation"]))
                    record["passed"] = not record["reasons"]
                    record["sql"] = plan.get("sql")
                    record["affected"] = plan.get("affected")
        elif case.get("expect") == "rows_or_reject":
            try:
                with connect(path) as connection:
                    gold_columns, gold_rows = _reference(connection, case["reference_sql"])
                result = run_read(path, case["prompt"], JevClient())
            except Exception as exc:
                if is_infrastructure_error(str(exc)):
                    raise
                record["passed"] = True
                record["error"] = str(exc)
                record["outcome"] = "rejected"
            else:
                record["sql"] = result["sql"]
                record["params"] = result["params"]
                reasons = _shape_ok(result["program"], result["sql"], case["shape"])
                ok, detail = _project(result["columns"], result["rows"], gold_columns, gold_rows,
                                      case.get("ordered", False))
                if not ok:
                    reasons.append(detail)
                record["reasons"] = reasons
                record["passed"] = not reasons
                record["outcome"] = "executed"
                record["calls"] = result["stats"]["jev_calls"]
        elif case.get("expect") == "reject":
            try:
                result = run_read(path, case["prompt"], JevClient())
            except Exception as exc:
                if is_infrastructure_error(str(exc)):
                    raise
                record["passed"] = True
                record["error"] = str(exc)
            else:
                if result.get("supported") is False:
                    record["passed"] = True
                else:
                    record["reasons"].append("request executed: " + str(result.get("sql")))
                    record["sql"] = result.get("sql")
        else:
            with connect(path) as connection:
                gold_columns, gold_rows = _reference(connection, case["reference_sql"])
            result = run_read(path, case["prompt"], JevClient())
            record["sql"] = result["sql"]
            record["params"] = result["params"]
            program = result["program"]
            reasons = _shape_ok(program, result["sql"], case["shape"])
            if case.get("scalar"):
                actual = result["rows"]
                if len(actual) != 1 or len(actual[0]) != 1 or len(gold_rows) != 1:
                    reasons.append(f"expected one scalar, got {actual[:3]}")
                elif _cell(actual[0][0]) != _cell(gold_rows[0][0]):
                    reasons.append(f"scalar {_cell(actual[0][0])!r} != {_cell(gold_rows[0][0])!r}")
            else:
                ok, detail = _project(result["columns"], result["rows"], gold_columns, gold_rows,
                                      case.get("ordered", False))
                if not ok:
                    reasons.append(detail)
            record["reasons"] = reasons
            record["passed"] = not reasons
            record["calls"] = result["stats"]["jev_calls"]
    except Exception as exc:
        record["error"] = str(exc)
        record["infrastructure"] = is_infrastructure_error(str(exc))
        record["passed"] = False
        if not record["infrastructure"]:
            record["reasons"].append(str(exc))
    record["seconds"] = round(time.perf_counter() - started, 3)
    return record


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/intentsql-independent-pack.json")
    if output.exists():
        raise SystemExit(f"Output exists: {output}")
    records = []
    for case in CASES:
        record = run_case(case)
        records.append(record)
        state = "PASS" if record["passed"] else ("INCONCLUSIVE" if record["infrastructure"] else "FAIL")
        print(f"{record['id']}: {state}" + ("" if record["passed"] else " " + "; ".join(record["reasons"][:3])),
              flush=True)
        output.write_text(json.dumps({"records": records}, indent=2, default=str))
    semantic = [item for item in records if not item["passed"] and not item["infrastructure"]]
    inconclusive = [item for item in records if item["infrastructure"]]
    print(json.dumps({
        "passed": sum(item["passed"] for item in records),
        "failed": len(semantic),
        "inconclusive": len(inconclusive),
        "total": len(records),
        "failure_ids": [item["id"] for item in semantic],
        "inconclusive_ids": [item["id"] for item in inconclusive],
    }, indent=2))
    return 1 if semantic else 0


if __name__ == "__main__":
    raise SystemExit(main())
