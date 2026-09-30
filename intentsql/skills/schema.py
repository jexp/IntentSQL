"""SQLite schema facts for the new semantic graph; no language interpretation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Column:
    name: str
    declared_type: str
    nullable: bool
    primary_key: bool


@dataclass(frozen=True)
class Relation:
    child_table: str
    child_column: str
    parent_table: str
    parent_column: str

    def other_table(self, table: str) -> str:
        if table == self.child_table:
            return self.parent_table
        if table == self.parent_table:
            return self.child_table
        raise ValueError("table is not part of this inspected relation")


@dataclass(frozen=True)
class NumericProfile:
    numeric_values: int
    nonnumeric_values: int

    @property
    def compatible(self) -> bool:
        total = self.numeric_values + self.nonnumeric_values
        # SQLite AVG/SUM coerce text; require strong evidence that the column
        # represents numbers before accepting that behavior for a text field.
        return self.numeric_values > 0 and self.numeric_values / total >= .95


def numeric_profile(connection: sqlite3.Connection, table: str, column: str) -> NumericProfile:
    """Inspect one selected column, without trusting its declared SQLite type."""
    if column not in {item.name for item in columns_for(connection, table)}:
        raise ValueError("Column is not in the inspected database")
    numeric = nonnumeric = 0
    sql = (f"SELECT {quote_identifier(column)} FROM {quote_identifier(table)} "
           f"WHERE {quote_identifier(column)} IS NOT NULL")
    for (value,) in connection.execute(sql):
        try:
            valid = Decimal(str(value)).is_finite()
        except (InvalidOperation, ValueError):
            valid = False
        if valid:
            numeric += 1
        else:
            nonnumeric += 1
    return NumericProfile(numeric, nonnumeric)


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))


def columns_for(connection: sqlite3.Connection, table: str) -> tuple[Column, ...]:
    if table not in table_names(connection):
        raise ValueError("table is not in the inspected database")
    rows = connection.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
    return tuple(Column(row[1], row[2] or "", not bool(row[3]), bool(row[5])) for row in rows)


def direct_relations(connection: sqlite3.Connection, table: str) -> tuple[Relation, ...]:
    """Return only real foreign-key edges adjacent to the inspected table."""
    names = table_names(connection)
    if table not in names:
        raise ValueError("table is not in the inspected database")
    edges: list[Relation] = []
    for child in names:
        for row in connection.execute(f"PRAGMA foreign_key_list({quote_identifier(child)})"):
            parent, child_column, parent_column = row[2], row[3], row[4]
            if parent not in names or not parent_column:
                continue
            if child == table or parent == table:
                edges.append(Relation(child, child_column, parent, parent_column))
    return tuple(edges)
