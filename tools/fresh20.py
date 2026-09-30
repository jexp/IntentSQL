#!/usr/bin/env python3
"""Fresh 20-case IntentSQL end-to-end stress run.

This is a thin case-pack wrapper around e2e.py.
It runs exactly 20 NEW natural-language cases through the real /api/run path.
No extra inspector/API checks are run in this pack.

Keep this file in the same directory as e2e.py.
"""

from __future__ import annotations

import sys

try:
    import e2e as suite
except ImportError:
    print(
        "ERROR: e2e.py must be in the same directory "
        "as this script (or importable on PYTHONPATH).",
        file=sys.stderr,
    )
    raise SystemExit(2)


def fresh20_cases(challenge_db: str | None, mutation_db: str | None) -> list[suite.Case]:
    cases: list[suite.Case] = [
        suite.Case(
            "N01", "COUNT with numeric equality", "cyberchase.db", "episodes",
            "How many episodes belong to season 2?",
            expected_scalar=14,
            params_contains=[2],
        ),
        suite.Case(
            "N02", "year-only date condition with a different year", "cyberchase.db", "episodes",
            "How many episodes originally aired during 2019?",
            expected_scalar=3,
            sql_not_contains=["LIKE"],
            params_contains=["2019-01-01", "2019-12-31"],
        ),
        suite.Case(
            "N03", "scalar MAX aggregate", "moneyball.db", "performances",
            "What is the largest home-run total recorded in a performance?",
            expected_scalar=73,
            sql_contains=["MAX"],
        ),
        suite.Case(
            "N04", "range filter + grouped MAX + HAVING + ordering", "moneyball.db", "performances",
            "From 1990 through 2005, show the years whose maximum home-run total was at least 60. Return year and maximum home runs, highest maximum first.",
            expected_rows=[[2001, 73], [1998, 70], [1999, 65]],
            params_contains=[1990, 2005, 60],
            sql_contains=["GROUP BY", "HAVING", "ORDER BY"],
        ),
        suite.Case(
            "N05", "categorical equality COUNT on DESE", "dese.db", "schools",
            "How many schools are in Worcester?",
            expected_scalar=49,
            params_contains=["Worcester"],
        ),
        suite.Case(
            "N06", "new direct one-hop FK join on DESE", "dese.db", None,
            "Show the five districts with the highest per-pupil expenditure. Return district name and per-pupil expenditure, highest first.",
            expected_count=5,
            expected_joined_tables=["expenditures", "districts"],
            sql_contains=[" JOIN ", "ORDER BY", "LIMIT"],
        ),
    ]

    if challenge_db:
        cases.extend([
            suite.Case(
                "N07", "two predicates + selected fields + descending ordering",
                challenge_db, "employees",
                "List Engineering employees earning more than 80000. Show name and salary, highest salary first.",
                expected_rows=[["Anas", 118000], ["Rachid", 112000], ["Karim", 86000]],
                params_contains=["Engineering", 80000],
                sql_contains=["ORDER BY"],
            ),
            suite.Case(
                "N08", "ISO-date inclusive range COUNT",
                challenge_db, "employees",
                "How many employees were hired from January 1, 2020 through December 31, 2022 inclusive?",
                expected_scalar=6,
                params_contains=["2020-01-01", "2022-12-31"],
                sql_contains=["BETWEEN"],
            ),
            suite.Case(
                "N09", "IS NULL with projected fields",
                challenge_db, "employees",
                "Show the name and department of employees who have no manager recorded, alphabetically by name.",
                expected_rows=[
                    ["Hassan", "Finance"],
                    ["Leila", "Support"],
                    ["Nadia", "Sales"],
                    ["Rachid", "Engineering"],
                ],
                sql_contains=["IS NULL", "ORDER BY"],
            ),
            suite.Case(
                "N10", "cross-column AND with <= and boolean-style value",
                challenge_db, "products",
                "Show the name and stock of products that are not discontinued and have stock no greater than 20, lowest stock first.",
                expected_rows=[
                    ["Halo Headset", 6],
                    ["Beacon Laptop", 9],
                    ["Delta Monitor", 14],
                    ["Kite Router", 16],
                    ["Echo Dock", 18],
                ],
                params_contains=[20],
                sql_contains=[" AND ", "ORDER BY"],
            ),
            suite.Case(
                "N11", "cross-column OR with projection and ordering",
                challenge_db, "products",
                "Show name, category, and price for products that are in Accessories or cost more than 500, most expensive first.",
                expected_rows=[
                    ["Beacon Laptop", "Computers", 1299.0],
                    ["Gamma Tablet", "Computers", 699.0],
                    ["Echo Dock", "Accessories", 149.0],
                    ["Halo Headset", "Accessories", 119.0],
                    ["Atlas Keyboard", "Accessories", 89.0],
                    ["Flux Webcam", "Accessories", 79.0],
                    ["Ion Charger", "Accessories", 49.0],
                    ["Cedar Mouse", "Accessories", 36.0],
                ],
                params_contains=["Accessories", 500],
                sql_contains=[" OR ", "ORDER BY"],
            ),
            suite.Case(
                "N12", "same-column alternatives / IN",
                challenge_db, "orders",
                "Show order ID, customer, and status for orders that were returned or cancelled, in ID order.",
                expected_rows=[
                    [5, "Marta", "returned"],
                    [11, "Sara", "cancelled"],
                    [13, "Youssef", "returned"],
                    [24, "Nora", "cancelled"],
                    [27, "Lina", "returned"],
                ],
                params_contains=["returned", "cancelled"],
                sql_contains=[" IN ", "ORDER BY"],
            ),
            suite.Case(
                "N13", "source filter + GROUP BY + COUNT + measure ordering",
                challenge_db, "orders",
                "For web orders only, count orders by country and show the busiest countries first.",
                expected_rows=[
                    ["France", 4],
                    ["Germany", 4],
                    ["Morocco", 3],
                    ["Spain", 2],
                    ["Canada", 1],
                ],
                params_contains=["web"],
                sql_contains=["GROUP BY", "ORDER BY"],
            ),
            suite.Case(
                "N14", "categorical filter + date range + COUNT",
                challenge_db, "orders",
                "How many completed orders were placed during February 2026?",
                expected_scalar=6,
                params_contains=["completed", "2026-02-01", "2026-02-28"],
            ),
            suite.Case(
                "N15", "three predicates + secondary ordering",
                challenge_db, "tickets",
                "For enterprise tickets that are resolved and have satisfaction at least 4, show ID, team, and satisfaction. Sort by satisfaction highest first, then ID lowest first.",
                expected_rows=[
                    [1, "Support", 5],
                    [3, "Engineering", 5],
                    [6, "Billing", 5],
                    [11, "Engineering", 5],
                    [16, "Engineering", 5],
                    [19, "Support", 5],
                    [24, "Engineering", 5],
                    [12, "Support", 4],
                    [17, "Billing", 4],
                ],
                params_contains=["enterprise", "resolved", 4],
                sql_contains=["ORDER BY"],
            ),
            suite.Case(
                "N16", "same-column alternatives + GROUP BY + HAVING",
                challenge_db, "tickets",
                "Among urgent or high-priority tickets, which teams have at least two tickets? Show team and count, largest count first.",
                expected_rows=[
                    ["Engineering", 8],
                    ["Support", 4],
                ],
                params_contains=["urgent", "high", 2],
                sql_contains=["GROUP BY", "HAVING", "ORDER BY"],
            ),
            suite.Case(
                "N17", "rounded AVG with source filter",
                challenge_db, "products",
                "What is the average rating of products that are not discontinued, rounded to two decimal places?",
                expected_scalar=4.45,
                sql_contains=["AVG", "ROUND"],
            ),
        ])

    if mutation_db:
        cases.extend([
            suite.Case(
                "N18", "UPDATE preview with new numeric value",
                mutation_db, "inventory",
                "Set the stock to 13 for the inventory item whose SKU is UNIQUE.",
                mode="change",
                expected_affected=1,
                expected_after_contains={"sku": "UNIQUE", "stock": 13},
                params_contains=[13, "UNIQUE"],
            ),
            suite.Case(
                "N19", "INSERT preview with five independently bound values",
                mutation_db, "inventory",
                "Add an inventory item with SKU FRESH20, name Twenty Run Item, status testing, stock 31, and price 52.25.",
                mode="change",
                expected_affected=1,
                expected_after_contains={
                    "sku": "FRESH20",
                    "name": "Twenty Run Item",
                    "status": "testing",
                    "stock": 31,
                    "price": 52.25,
                },
                params_contains=["FRESH20", "Twenty Run Item", "testing", 31, 52.25],
            ),
            suite.Case(
                "N20", "DELETE preview with a new natural-language paraphrase",
                mutation_db, "inventory",
                "Remove the inventory item identified by SKU UNIQUE.",
                mode="change",
                expected_affected=1,
                params_contains=["UNIQUE"],
                sql_contains=["DELETE"],
            ),
        ])

    return cases


# Replace the v2 case matrix with this fresh 20-case pack.
suite.build_cases = fresh20_cases

# This pack covers NL cases; tools/e2e.py also runs inspector/API checks.
suite.api_feature_checks = lambda base, databases, timeout: []

if __name__ == "__main__":
    raise SystemExit(suite.main())
