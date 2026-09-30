"""Development-only live CRUD semantic matrix on a disposable SQLite fixture."""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intentsql import mutations, read_engine  # noqa: E402
from intentsql.database import connect  # noqa: E402
from intentsql.jev_client import JevClient  # noqa: E402
from intentsql.semantic_read import run_read  # noqa: E402


CASES = [
    ("text_eq", "whose title is Alpha", "title = 'Alpha'"),
    ("number_eq", "in season 2", "season = 2"),
    ("number_gt", "with rating greater than 7", "rating > 7"),
    ("number_range", "with rating from 5 to 9", "rating BETWEEN 5 AND 9"),
    ("full_date", "aired on 2023-04-01", "air_date = '2023-04-01'"),
    ("year", "aired in 2023", "air_date BETWEEN '2023-01-01' AND '2023-12-31'"),
    ("month_year", "aired in April 2023", "air_date BETWEEN '2023-04-01' AND '2023-04-30'"),
    ("date_range", "from 2023-01-01 to 2023-09-01", "air_date BETWEEN '2023-01-01' AND '2023-09-01'"),
    ("alternatives", "in season 1 or 2", "season IN (1, 2)"),
    ("exclusion", "whose category is not Drama", "category != 'Drama'"),
    ("null", "whose note is missing", "note IS NULL"),
    ("not_null", "whose note is present", "note IS NOT NULL"),
    ("contains", "whose title contains Star", "title LIKE '%Star%'"),
    ("prefix", "whose title starts with Star", "title LIKE 'Star%'"),
    ("suffix", "whose title ends with One", "title LIKE '%One'"),
    ("and", "in season 2 with rating greater than 7", "season = 2 AND rating > 7"),
    ("or", "whose category is Drama or Comedy", "category IN ('Drama', 'Comedy')"),
    ("category", "in the Comedy category", "category = 'Comedy'"),
    ("code", "with code DRA", "code = 'DRA'"),
    ("typo", "in the Comdey category", "category = 'Comedy'"),
    ("single", "whose id is 3", "id = 3"),
    ("cross_or", "in season 1 or with rating above 9", "season = 1 OR rating > 9"),
    ("abbreviation", "with category Sci", "category = 'Science Fiction'"),
    ("numeric_exclusion", "whose season is not 2", "season != 2"),
]

INSERT_CASES = [
    ("insert_explicit", "Add an inventory row with id 3, name Gamma, and stock 12.",
     {"id": 3, "name": "Gamma", "stock": 12}),
    ("insert_unquoted", "Insert an inventory item with id 4, name Delta, stock 0, and category hardware.",
     {"id": 4, "name": "Delta", "stock": 0, "category": "hardware"}),
    ("insert_lowercase_phrase", "Add an inventory row with id 6, name new test item, stock 5.",
     {"id": 6, "name": "new test item", "stock": 5}),
    ("insert_quoted", "Add an inventory row with id 5, name \"O'Reilly\", and stock 7.",
     {"id": 5, "name": "O'Reilly", "stock": 7}),
]


def make_fixture(path: Path) -> None:
    with connect(path) as conn:
        conn.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, title TEXT, air_date TEXT, "
                     "season INTEGER, rating INTEGER, category TEXT, code TEXT, note TEXT, flag INTEGER)")
        conn.executemany("INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)", [
            (1, "Alpha", "2020-03-10", 1, 5, "Drama", "DRA", None),
            (2, "Star One", "2021-05-20", 1, 8, "Comedy", "COM", "pilot"),
            (3, "Star Two", "2022-07-13", 2, 9, "Drama", "DRA", "special"),
            (4, "Beta One", "2023-04-01", 2, 7, "Comedy", "COM", None),
            (5, "Star Three", "2023-08-15", 3, 10, "Drama", "DRA", "finale"),
            (6, "Gamma", "2023-10-03", 3, 4, "Science Fiction", "SCI", None),
            (7, "Delta", "2024-01-05", 4, 6, "Comedy", "COM", "bonus"),
            (8, "A Star Journey", "2024-02-01", 4, 6, "Drama", "DRA", None),
            (9, "One More", "2024-03-01", 4, 6, "Comedy", "COM", "extra"),
        ])


def run_matrix(names: set[str], operations: set[str], trace: bool = False) -> dict:
    results = []
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "matrix.sqlite"
        make_fixture(path)
        for name, fragment, gold_where in CASES:
            if names and name not in names:
                continue
            with connect(path) as conn:
                gold = [row[0] for row in conn.execute(
                    "SELECT id FROM episodes WHERE " + gold_where + " ORDER BY id")]
            for operation in ("SELECT", "UPDATE", "DELETE"):
                if operation not in operations:
                    continue
                question = {"SELECT": f"Show all episodes {fragment}.",
                            "UPDATE": f"Set flag to 1 for episodes {fragment}.",
                            "DELETE": f"Delete all episodes {fragment}."}[operation]
                start = time.perf_counter()
                events = []
                try:
                    if operation == "SELECT":
                        client = JevClient(on_event=events.append)
                        result = run_read(path, question, client, on_step=events.append)
                        actual = sorted(row[0] for row in result["rows"])
                        usage = client.usage_stats()
                        supported = True
                    else:
                        read_engine.EVENT_SINK = events.append
                        result = mutations.plan_mutation(path, question)
                        actual = sorted(row["id"] for row in result["before"])
                        usage = read_engine.usage_stats()
                        supported = result["supported"]
                    row = {"class": name, "operation": operation,
                           "passed": supported and actual == gold,
                           "supported": supported, "actual": actual, "gold": gold,
                           "sql": result["sql"], "params": result["params"],
                           "calls": usage.get("jev_calls", 0),
                           "input_tokens": usage.get("input_tokens", 0),
                           "output_tokens": usage.get("output_tokens", 0)}
                except Exception as exc:
                    usage = read_engine.usage_stats() if operation != "SELECT" else client.usage_stats()
                    row = {"class": name, "operation": operation, "passed": False,
                           "error": str(exc), "gold": gold,
                           "calls": usage.get("jev_calls", 0),
                           "input_tokens": usage.get("input_tokens", 0),
                           "output_tokens": usage.get("output_tokens", 0)}
                finally:
                    read_engine.EVENT_SINK = None
                if trace and not row["passed"]:
                    row["trace"] = events
                row["latency_seconds"] = round(time.perf_counter() - start, 3)
                results.append(row)
                print(json.dumps({key: value for key, value in row.items() if key != "trace"},
                                 ensure_ascii=False), flush=True)
        if "INSERT" in operations:
            with connect(path) as conn:
                conn.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY, name TEXT, "
                             "stock INTEGER, category TEXT)")
                conn.executemany("INSERT INTO inventory VALUES (?, ?, ?, ?)", [
                    (1, "Alpha", 8, "hardware"), (2, "Beta", 4, "software")])
            for name, question, expected in INSERT_CASES:
                if names and name not in names:
                    continue
                start = time.perf_counter()
                events = []
                read_engine.EVENT_SINK = events.append
                try:
                    result = mutations.plan_mutation(path, question)
                    actual = result["program"]["assignments"]
                    supported = result["supported"]
                    row = {"class": name, "operation": "INSERT",
                           "passed": supported and actual == expected and result["affected"] == 1,
                           "supported": supported, "actual": actual, "gold": expected,
                           "sql": result["sql"], "params": result["params"]}
                except Exception as exc:
                    row = {"class": name, "operation": "INSERT", "passed": False,
                           "error": str(exc), "gold": expected}
                finally:
                    read_engine.EVENT_SINK = None
                usage = read_engine.usage_stats()
                row.update(calls=usage.get("jev_calls", 0),
                           input_tokens=usage.get("input_tokens", 0),
                           output_tokens=usage.get("output_tokens", 0),
                           latency_seconds=round(time.perf_counter() - start, 3))
                if trace and not row["passed"]:
                    row["trace"] = events
                results.append(row)
                print(json.dumps({key: value for key, value in row.items() if key != "trace"},
                                 ensure_ascii=False), flush=True)
    return {"passed": sum(row["passed"] for row in results), "total": len(results),
            "calls": sum(row["calls"] for row in results),
            "input_tokens": sum(row["input_tokens"] for row in results),
            "output_tokens": sum(row["output_tokens"] for row in results),
            "median_latency": statistics.median(row["latency_seconds"] for row in results),
            "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--classes", nargs="*", default=[])
    parser.add_argument("--operations", nargs="*", default=["SELECT", "UPDATE", "DELETE", "INSERT"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    report = run_matrix(set(args.classes), set(args.operations), args.trace)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}))
