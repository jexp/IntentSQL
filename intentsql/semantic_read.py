"""Small Jev-skill graph for bounded, single-table read queries."""

from __future__ import annotations

from intentsql.database import connect
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import re
from typing import Any, Callable

from intentsql.jev_client import JevClient
from intentsql.decision_context import DecisionClient, ReadState, Stage
from intentsql.skills.entity import Entity, explicitly_named_source, resolve_entity
from intentsql.skills.facts import extract_facts
from intentsql.skills.fields import resolve_distinct_target, resolve_fields
from intentsql.skills.intent import route_intent
from intentsql.skills.ordering import (resolve_ordering, resolve_single_row_order_need,
                                       review_order_direction)
from intentsql.skills.predicate import resolve_predicate_operator
from intentsql.capabilities import validate_operand
from intentsql.skills.predicate_value import resolve_predicate_value, has_request_evidence
from intentsql.skills.predicate_columns import resolve_predicate_columns, refine_predicate_candidates
from intentsql.skills.role_evidence import explicitly_names_column as _explicitly_names_column
from intentsql.skills.condition_logic import resolve_condition_logic
from intentsql.skills.aggregate import resolve_aggregate
from intentsql.skills.aggregate_shape import resolve_aggregate_shape
from intentsql.skills.field_shape import resolve_field_shape
from intentsql.skills.predicate import column_affinity
from intentsql.skills.grouping import resolve_group_key, resolve_extra_group_key
from intentsql.skills.group_ordering import resolve_group_ordering
from intentsql.skills.relationship_need import RelationshipNeed, resolve_relationship_need
from intentsql.skills.filter_need import FilterNeed, resolve_filter_need
from intentsql.skills.filter_repair import resolve_missing_null_filter
from intentsql.skills.group_measure import resolve_group_measure
from intentsql.skills.having import resolve_having
from intentsql.skills.having_measure import resolve_having_measure
from intentsql.skills.quantity import resolve_quantity
from intentsql.skills.range_value import (RangeBounds, column_uses_iso_dates,
                                          explicit_inclusive_interval,
                                          resolve_range_bounds, strict_date_bounds)
from intentsql.skills.schema import (Relation, columns_for, direct_relations,
                                  numeric_profile, quote_identifier, table_names)
from intentsql.skills.value_hints import (mechanically_related_values, small_category_hints, targeted_values,
                                          profiled_text_patterns)
from intentsql.skills.relation import resolve_related_table
from intentsql.skills.join_fields import QualifiedField, resolve_join_fields
from intentsql.skills.join_predicate import (joined_predicate_candidates,
                                             resolve_join_predicate_columns)
from intentsql.skills.rounding import resolve_rounding
from intentsql.skills.join_ordering import resolve_join_ordering
from intentsql.skills.stored_output import resolve_stored_output
from intentsql.skills.coverage import resolve_coverage, review_unique_result_filter
from intentsql.skills.alternatives import resolve_alternatives
from intentsql.skills.group_clauses import resolve_group_clauses, threshold_candidates
from intentsql.skills.count_shape import resolve_count_shape
from intentsql.skills.predicate_completeness import resolve_predicate_completion
from intentsql.skills.result_cardinality import resolve_result_cardinality
from intentsql.skills.per_group_extremum import (resolve_per_group_extremum,
                                                  should_probe_per_group_shape)
from intentsql.skills.relation_absence import (resolve_relation_absence,
                                               should_probe_relation_absence)


MAX_LOADED_ROWS = 5000
_COMPARISONS = frozenset(("=", "!=", ">", ">=", "<", "<="))

# A correlated per-group selector needs a second reference to the same table.
# Alias only that inner copy; keep the outer table under its real schema name so
# generated SQL stays ordinary and easy to inspect.
_PARTITION_GROUP_ALIAS = "grouped"
_FUNCTION_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "of", "on", "or", "the", "to", "was", "were", "what", "when",
    "where", "which", "who", "with",
})


def _table_for_exact_quoted_facts(conn, schema_columns, facts):
    """Return the sole table containing an exact quoted request operand."""
    quoted = tuple(fact for fact in facts if fact.kind == "quoted_text")
    if not quoted:
        return None
    matching: set[str] = set()
    for table, columns in schema_columns.items():
        for column in columns:
            for fact in quoted:
                row = conn.execute(
                    f"SELECT 1 FROM {quote_identifier(table)} "
                    f"WHERE CAST({quote_identifier(column.name)} AS TEXT) = ? LIMIT 1",
                    (fact.value,)).fetchone()
                if row:
                    matching.add(table)
    return next(iter(matching)) if len(matching) == 1 else None


def _columns_for_exact_quoted_facts(conn, table, columns, facts):
    """Mechanically locate exact quoted operands inside one established table."""
    quoted = tuple(fact for fact in facts if fact.kind == "quoted_text")
    matching: list[str] = []
    for column in columns:
        if any(conn.execute(
                f"SELECT 1 FROM {quote_identifier(table)} "
                f"WHERE CAST({quote_identifier(column.name)} AS TEXT) = ? LIMIT 1",
                (fact.value,)).fetchone() for fact in quoted):
            matching.append(column.name)
    return tuple(matching)


def _has_exact_value_evidence(request: str, values: tuple[str, ...]) -> bool:
    text = request.casefold()
    return any(re.search(r"(?<!\w)" + re.escape(str(value).strip().casefold()) +
                         r"(?!\w)", text) for value in values if str(value).strip())


def _row_equivalent_value_column(conn, table, columns, value_evidence):
    """Choose the first schema column only when exact predicates match identical rows."""
    if len(columns) < 2:
        return columns[0] if columns else None
    values = [str(value_evidence[name][0]) for name in columns]
    if len({value.strip().casefold() for value in values}) != 1:
        return None
    row_sets = []
    try:
        for name, value in zip(columns, values):
            rows = conn.execute(
                f"SELECT rowid FROM {quote_identifier(table)} "
                f"WHERE CAST({quote_identifier(name)} AS TEXT) = ?", (value,)).fetchall()
            row_sets.append(tuple(row[0] for row in rows))
    except Exception:
        return None
    return columns[0] if row_sets and row_sets[0] and len(set(row_sets)) == 1 else None


def _neighbor_evidence(request: str, source: str, source_columns,
                       related: dict[str, tuple[Any, ...]]) -> bool:
    """Find request words grounded only in an inspected adjacent relation."""
    def words(value: str) -> set[str]:
        return {word[:-1] if word.endswith("s") and len(word) > 4 else word
                for word in re.findall(r"[a-z]{4,}", value.casefold().replace("_", " "))}

    request_words = words(request)
    local = words(source) | set().union(*(words(col.name) for col in source_columns
                                          if not col.name.casefold().endswith("_id")))
    for table, columns in related.items():
        neighbor = words(table) | set().union(*(words(col.name) for col in columns))
        if request_words & (neighbor - local):
            return True
    return False


def _matching_category_evidence(request: str,
                                domains: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """Retrieve complete small-domain labels whose words occur in the request."""
    def words(value: str) -> set[str]:
        return {word[:-1] if word.endswith("s") and len(word) > 4 else word
                for word in re.findall(r"[a-z]{3,}", value.casefold())}

    requested = words(request)
    return {column: tuple(value for value in values
                          if len(words(value)) >= 2 and words(value) <= requested)
            for column, values in domains.items()
            if any(len(words(value)) >= 2 and words(value) <= requested
                   for value in values)}


def _prefer_explicit_condition_order(request: str, conditions: list[Any]) -> list[Any]:
    """Put explicitly named predicate columns before semantic lookalikes.

    This makes duplicate resolution order-independent: if two candidate columns
    bind the same operand/operator but the request explicitly names only one,
    the explicit schema role becomes the established predicate.
    """
    return sorted(conditions,
                  key=lambda condition: not _explicitly_names_column(
                      request, condition.column))


def _drop_shadowed_duplicate_conditions(request: str,
                                        conditions: list[Any]) -> list[Any]:
    """Drop an unmentioned lookalike when an explicit column binds the same predicate."""
    explicit = [condition for condition in conditions
                if _explicitly_names_column(request, condition.column)]
    return [condition for condition in conditions
            if (_explicitly_names_column(request, condition.column) or
                not any(prior.operator == condition.operator and
                        prior.value == condition.value
                        for prior in explicit))]



def _is_scalar_aggregate_result(output_kind: str, grouping: bool) -> bool:
    """Return whether the typed result is one scalar aggregate row."""
    return output_kind in {"count", "other"} and not grouping


def _can_apply_row_limit(output_kind: str, grouping: bool) -> bool:
    """Only multi-row result shapes can meaningfully consume a global LIMIT."""
    return not _is_scalar_aggregate_result(output_kind, grouping)


def _should_retry_missing_join_filter(category: str, relation_edge: Relation | None,
                                      conditions: list[Any]) -> bool:
    """Allow one bounded joined-predicate retry only when none was grounded initially."""
    return category == "missing_filter" and relation_edge is not None and not conditions


def _can_reanchor_scalar_aggregate(output_kind: str, grouping: bool,
                                   has_group_threshold: bool,
                                   has_filters: bool) -> bool:
    return (output_kind == "other" and not grouping and
            not has_group_threshold and not has_filters)


def _keep_global_extremum_for_limit(global_extremum: bool,
                                    limit: int | None) -> bool:
    """A global MIN/MAX row selector is incompatible with an explicit top-N > 1."""
    return global_extremum and (limit is None or limit <= 1)


def _can_append_conjunctive_filter(connector: str, condition_count: int) -> bool:
    """The flat IR cannot represent (A OR B) AND C without changing meaning."""
    return connector == "AND" or condition_count <= 1


def _count_depends_on_unnamed_neighbor(output_kind: str, neighbor_evidence: bool,
                                      grounded_relation: bool,
                                      explicit_local_fk: bool) -> bool:
    """A row tally is not local when its only schema word lives on a neighbor.

    Naming the related table is a separate, stronger signal. This predicate
    covers the remaining case: the request word matches a neighbor column,
    the relationship was not established by the table name, and compiling
    COUNT on the source table would drop that measure.
    """
    return (output_kind == "count" and neighbor_evidence
            and not grounded_relation and not explicit_local_fk)


def _can_probe_unique_source_row(output: str, group_column: str | None,
                                 aggregate_function: str | None,
                                 partition_column: str | None) -> bool:
    """A one-row evidence probe is only a legal ungrouped field or row read.

    Replacing a grouped aggregate's output with `rows` while leaving GROUP BY
    in place is not a source-row probe. It is an illegal compiler shape, and a
    single grouped result does not prove that a source filter is complete.
    """
    return (output in {"fields", "rows"} and not group_column
            and not aggregate_function and not partition_column)


def _literal_facts_require_source_filter(
        request: str, facts: tuple[Any, ...], *, output_kind: str,
        grouping: bool, has_group_threshold: bool,
        having_value: int | float | None = None,
        round_places: int | None = None) -> bool:
    """Preserve exact request literals when the coarse filter route misses them.

    Literal extraction does not assign semantic roles.  This helper only marks
    cases where a row-limit interpretation is structurally impossible (a scalar
    aggregate), or where explicit interval syntax establishes a source range
    before grouping.  Column/operator/value binding still belongs to the normal
    bounded predicate planner.
    """
    assigned_values = {value for value in (having_value, round_places)
                       if value is not None}
    remaining = tuple(
        fact for fact in facts
        if fact.kind in {"integer", "number", "worded_number",
                         "date", "month_year", "quoted_text"}
        and fact.value not in assigned_values)
    if not remaining:
        return False
    if _is_scalar_aggregate_result(output_kind, grouping):
        return True
    if grouping and not has_group_threshold:
        numeric = tuple(fact for fact in remaining
                        if fact.kind in {"integer", "number"})
        return explicit_inclusive_interval(request, numeric) is not None
    return False


@dataclass(frozen=True)
class Condition:
    column: str
    operator: str
    value: int | str | tuple[int | str, ...] | None
    table: str | None = None


@dataclass(frozen=True)
class SelectQuery:
    table: str
    output: str
    columns: tuple[str, ...] = ()
    conditions: tuple[Condition, ...] = ()
    connector: str = "AND"
    order_column: str | None = None
    order_direction: str | None = None
    limit: int | None = None
    distinct: bool = False
    aggregate_function: str | None = None
    aggregate_column: str | None = None
    group_column: str | None = None
    group_order_target: str | None = None
    group_order_direction: str | None = None
    group_tie_direction: str | None = None
    having_operator: str | None = None
    having_value: int | None = None
    relation: Relation | None = None
    qualified_columns: tuple[QualifiedField, ...] = ()
    round_places: int | None = None
    order_table: str | None = None
    extra_group_column: str | None = None
    group_secondary_target: str | None = None
    group_secondary_direction: str | None = None
    secondary_order_column: str | None = None
    secondary_order_direction: str | None = None
    partition_column: str | None = None
    partition_extremum_column: str | None = None
    partition_extremum_function: str | None = None
    global_extremum_table: str | None = None
    global_extremum_column: str | None = None
    global_extremum_function: str | None = None
    having_aggregate_function: str | None = None
    having_aggregate_column: str | None = None


def compile_conditions(conditions: tuple[Condition, ...], connector: str,
                       valid_columns: tuple[str, ...],
                       joined_columns: dict[str, tuple[str, ...]] | None = None
                       ) -> tuple[str, list[Any]]:
    """Compile the same typed row predicate for SELECT, UPDATE, and DELETE."""
    if conditions and connector not in {"AND", "OR"}:
        raise ValueError("Invalid Boolean connector")
    allowed = {None: set(valid_columns)}
    allowed.update({table: set(names) for table, names in (joined_columns or {}).items()})
    clauses: list[str] = []
    params: list[Any] = []
    for condition in conditions:
        if condition.table not in allowed or condition.column not in allowed[condition.table]:
            raise ValueError("Predicate references an uninspected column")
        name = ((quote_identifier(condition.table) + ".") if condition.table else "") + quote_identifier(condition.column)
        op = condition.operator
        validate_operand(op, condition.value)
        if op in {"IS NULL", "IS NOT NULL"}:
            if condition.value is not None:
                raise ValueError("NULL comparison cannot have an operand")
            clause = name + " " + op
        elif op in _COMPARISONS:
            if condition.value is None or isinstance(condition.value, tuple):
                raise ValueError("Comparison requires one resolved operand")
            clause = name + " " + op + " ?"
            params.append(condition.value)
        elif op in {"BETWEEN", "IN"}:
            values = condition.value
            if not isinstance(values, tuple) or not values or (op == "BETWEEN" and len(values) != 2):
                raise ValueError("Set comparison requires resolved operands")
            if any(value is None for value in values):
                raise ValueError("Set comparison cannot contain NULL")
            clause = (name + " BETWEEN ? AND ?" if op == "BETWEEN" else
                      name + " IN (" + ", ".join("?" for _ in values) + ")")
            params.extend(values)
        elif op in {"CONTAINS", "PREFIX", "SUFFIX"}:
            if not isinstance(condition.value, str):
                raise ValueError("Text comparison requires a resolved string")
            value = condition.value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = {"CONTAINS": f"%{value}%", "PREFIX": f"{value}%", "SUFFIX": f"%{value}"}[op]
            clause = name + " LIKE ? ESCAPE '\\'"
            params.append(pattern)
        else:
            raise ValueError("Unsupported comparison operator")
        clauses.append(clause)
    return ((clauses[0] if len(clauses) == 1 else
             f" {connector} ".join(f"({clause})" for clause in clauses)) if clauses else "", params)


def compile_select(query: SelectQuery, valid_columns: tuple[str, ...],
                   joined_columns: dict[str, tuple[str, ...]] | None = None) -> tuple[str, list[Any]]:
    """Identifiers come only from inspected schema; values are bound parameters."""
    valid = set(valid_columns)
    for name in (*query.columns, (query.order_column if query.order_table is None else None),
                 query.secondary_order_column, query.aggregate_column, query.group_column, query.extra_group_column,
                 query.partition_column, query.partition_extremum_column,
                 query.having_aggregate_column,
                 *(condition.column for condition in query.conditions if condition.table is None)):
        if name is not None and name not in valid:
            raise ValueError("Query references an uninspected column")
    joined_columns = joined_columns or {}
    if query.relation:
        joined_table = query.relation.other_table(query.table)
        if joined_table not in joined_columns:
            raise ValueError("Joined table schema was not inspected")
        joined_field_output = query.output == "fields" and bool(query.qualified_columns)
        base_row_output = query.output == "rows" and not query.qualified_columns
        if (not (joined_field_output or base_row_output) or
                query.group_column or query.aggregate_function):
            raise ValueError("This direct-join compiler supports base rows or chosen joined fields")
    elif query.qualified_columns or query.order_table or any(condition.table for condition in query.conditions):
        raise ValueError("Qualified columns require a validated join")
    if query.partition_column:
        if (query.output not in {"fields", "rows"} or query.relation or query.group_column or
                not query.partition_extremum_column or
                query.partition_extremum_function not in {"MIN", "MAX"}):
            raise ValueError("Invalid per-group extremum query")
    elif query.partition_extremum_column or query.partition_extremum_function:
        raise ValueError("Incomplete per-group extremum query")
    allowed = {query.table: valid, **{table: set(cols) for table, cols in joined_columns.items()}}
    if query.global_extremum_column:
        if (query.output != "fields" or query.group_column or
                query.global_extremum_function not in {"MIN", "MAX"}):
            raise ValueError("Invalid global row-extremum query")
        metric_table = query.global_extremum_table or query.table
        if (metric_table not in allowed or
                query.global_extremum_column not in allowed[metric_table]):
            raise ValueError("Global extremum references an uninspected column")
    elif query.global_extremum_table or query.global_extremum_function:
        raise ValueError("Incomplete global row-extremum query")
    for field in query.qualified_columns:
        if field.table not in allowed or field.column not in allowed[field.table]:
            raise ValueError("Query references an uninspected qualified output")
    for condition in query.conditions:
        if condition.table and (condition.table not in allowed or
                                condition.column not in allowed[condition.table]):
            raise ValueError("Query references an uninspected qualified condition")
    if query.order_table and (query.order_table not in allowed or
                              query.order_column not in allowed[query.order_table]):
        raise ValueError("Query references an uninspected qualified sort column")
    if query.output == "aggregate":
        if query.aggregate_function not in ("AVG", "SUM", "MIN", "MAX") or not query.aggregate_column:
            raise ValueError("Incomplete scalar aggregate")
        measure_expr = query.aggregate_function + "(" + quote_identifier(query.aggregate_column) + ")"
        if query.round_places is not None:
            if not 0 <= query.round_places <= 6:
                raise ValueError("Rounding precision outside supported range")
            measure_expr = "ROUND(" + measure_expr + ", ?)"
        projection = (measure_expr + " AS " +
                      quote_identifier(query.aggregate_function.lower() + "_" + query.aggregate_column))
    elif query.output == "count":
        if query.distinct:
            if len(query.columns) != 1:
                raise ValueError("Counting distinct values requires one selected column")
            measure_expr = "COUNT(DISTINCT " + quote_identifier(query.columns[0]) + ")"
        else:
            measure_expr = "COUNT(*)"
        projection = measure_expr + " AS row_count"
    elif query.output == "rows":
        projection = (quote_identifier(query.table) + ".*"
                      if query.relation else "*")
    elif query.output == "fields" and query.qualified_columns:
        projection = ", ".join(
            quote_identifier(field.table) + "." + quote_identifier(field.column) +
            " AS " + quote_identifier(field.table + "_" + field.column)
            for field in query.qualified_columns)
    elif query.output == "fields" and query.columns:
        projection = ", ".join(quote_identifier(name) for name in query.columns)
    else:
        raise ValueError("Incomplete output selection")
    if query.round_places is not None and query.output != "aggregate":
        raise ValueError("Rounding currently requires a supported aggregate measure")
    if query.group_column:
        if query.output not in ("count", "aggregate"):
            raise ValueError("Grouped field projection is not supported yet")
        projection = ", ".join(quote_identifier(name) for name in
                                (query.group_column, query.extra_group_column) if name) + ", " + projection
    if query.distinct and query.output not in ("fields", "count"):
        raise ValueError("DISTINCT currently requires chosen output fields")
    sql = "SELECT " + ("DISTINCT " if query.distinct and query.output == "fields" else "") + projection
    sql += " FROM " + quote_identifier(query.table)
    if query.relation:
        edge = query.relation
        sql += (" JOIN " + quote_identifier(edge.other_table(query.table)) +
                " ON " + quote_identifier(edge.child_table) + "." + quote_identifier(edge.child_column) +
                " = " + quote_identifier(edge.parent_table) + "." + quote_identifier(edge.parent_column))
    params: list[Any] = [query.round_places] if query.round_places is not None else []
    predicate_sql, predicate_params = compile_conditions(
        query.conditions, query.connector, valid_columns,
        {table: tuple(names) for table, names in allowed.items()})
    params.extend(predicate_params)
    clauses: list[str] = [predicate_sql] if predicate_sql else []
    if query.partition_column:
        base = quote_identifier(query.table)
        grouped = quote_identifier(_PARTITION_GROUP_ALIAS)
        target = quote_identifier(query.partition_extremum_column)
        group = quote_identifier(query.partition_column)
        clauses.append(
            f"{base}.{target} = (SELECT {query.partition_extremum_function}({grouped}.{target}) "
            f"FROM {quote_identifier(query.table)} AS {grouped} "
            f"WHERE {grouped}.{group} = {base}.{group})")
    if query.global_extremum_column:
        metric_table = query.global_extremum_table or query.table
        qualified = quote_identifier(metric_table) + "." + quote_identifier(query.global_extremum_column)
        clauses.append(
            f"{qualified} = (SELECT {query.global_extremum_function}({quote_identifier(query.global_extremum_column)}) "
            f"FROM {quote_identifier(metric_table)})")
    if clauses:
        sql += " WHERE " + (clauses[0] if len(clauses) == 1 else
                            f" {query.connector} ".join(f"({clause})" for clause in clauses))
    if query.group_column:
        sql += " GROUP BY " + ", ".join(quote_identifier(name) for name in
                                      (query.group_column, query.extra_group_column) if name)
    if query.having_operator:
        if not query.group_column or query.output not in ("count", "aggregate"):
            raise ValueError("HAVING requires a grouped aggregate")
        if query.having_operator not in _COMPARISONS or query.having_value is None:
            raise ValueError("Incomplete grouped threshold")
        having_expr = measure_expr
        if query.having_aggregate_function:
            if query.having_aggregate_function == "COUNT" and not query.having_aggregate_column:
                having_expr = "COUNT(*)"
            elif (query.having_aggregate_function in {"COUNT", "SUM", "AVG", "MIN", "MAX"}
                  and query.having_aggregate_column in valid):
                having_expr = (query.having_aggregate_function + "(" +
                               quote_identifier(query.having_aggregate_column) + ")")
            else:
                raise ValueError("HAVING references an unregistered aggregate measure")
        sql += " HAVING " + having_expr + " " + query.having_operator + " ?"
        params.append(query.having_value)
    if query.group_order_target:
        if not query.group_column or query.group_order_target not in ("key", "measure"):
            raise ValueError("Invalid grouped ordering target")
        if query.group_order_direction not in ("ASC", "DESC"):
            raise ValueError("Invalid grouped ordering direction")
        if query.group_tie_direction not in (None, "ASC", "DESC"):
            raise ValueError("Invalid grouped tie direction")
        metric_alias = ("row_count" if query.output == "count" else
                        query.aggregate_function.lower() + "_" + query.aggregate_column)
        primary = query.group_column if query.group_order_target == "key" else metric_alias
        sql += " ORDER BY " + quote_identifier(primary) + " " + query.group_order_direction
        if query.group_tie_direction:
            if query.group_order_target != "measure":
                raise ValueError("Tie ordering requires aggregate metric first")
            sql += ", " + quote_identifier(query.group_column) + " " + query.group_tie_direction
        elif query.group_order_target == "measure":
            sql += ", " + quote_identifier(query.group_column) + " ASC"
        if query.group_secondary_target == "measure":
            metric_alias = ("row_count" if query.output == "count" else
                            query.aggregate_function.lower() + "_" + query.aggregate_column)
            sql += ", " + quote_identifier(metric_alias) + " " + (query.group_secondary_direction or "ASC")
        if query.extra_group_column:
            sql += ", " + quote_identifier(query.extra_group_column) + " ASC"
    elif query.group_column and query.extra_group_column:
        sql += " ORDER BY " + quote_identifier(query.group_column) + " ASC, " + quote_identifier(query.extra_group_column) + " ASC"
    if query.order_column:
        if query.order_direction not in ("ASC", "DESC"):
            raise ValueError("Invalid sort direction")
        order_sql = ((quote_identifier(query.order_table) + ".") if query.order_table else "")
        ordering_terms = [order_sql + quote_identifier(query.order_column) + " " + query.order_direction]
        if query.secondary_order_column:
            if query.order_table:
                raise ValueError("Secondary ordering is currently supported for single-table reads only")
            if query.secondary_order_direction not in ("ASC", "DESC"):
                raise ValueError("Invalid secondary sort direction")
            if query.secondary_order_column == query.order_column:
                raise ValueError("Secondary sort key must differ from primary sort key")
            ordering_terms.append(quote_identifier(query.secondary_order_column) + " " + query.secondary_order_direction)
        sql += " ORDER BY " + ", ".join(ordering_terms)
    if query.limit is not None:
        if query.limit < 0 or query.limit > 100_000:
            raise ValueError("Result limit outside allowed range")
        sql += " LIMIT ?"
        params.append(query.limit)
    return sql, params



@dataclass(frozen=True)
class ConditionPlan:
    conditions: tuple[Condition, ...]
    connector: str
    grounded_pattern_bindings: tuple[dict[str, Any], ...]


def plan_conditions(conn, request: str, entity: Entity, columns: tuple[Any, ...],
                    client: DecisionClient, facts: tuple[Any, ...],
                    record: Callable[[str, Any, Any], None],
                    profiled: Callable[[str, str], Any], *,
                    needs_filter: bool, output_kind: str = "rows",
                    output_fields: tuple[str, ...] = (),
                    grouping: bool = False, aggregate_function: str | None = None,
                    aggregate_column: str | None = None,
                    has_group_threshold: bool = False,
                    relation_edge: Relation | None = None,
                    joined_table: str | None = None,
                    joined_table_columns: tuple[Any, ...] = (),
                    qualified_output_fields: tuple[QualifiedField, ...] = (),
                    qualified_order_field: QualifiedField | None = None,
                    excluded_columns: tuple[str, ...] = (),
                    established_columns: tuple[str, ...] = (),
                    excluded_fact_ids: tuple[str, ...] = ()) -> ConditionPlan:
    """Shared schema-grounded row condition planner for reads and writes.

    The caller establishes the operation and source. This stage resolves only
    row predicates and their Boolean connector from inspected evidence.
    """
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def values_for(table, column, extrema=False):
        return targeted_values(conn, table, column, request, include_extrema=extrema,
                               include_common=True)

    @lru_cache(maxsize=None)
    def patterns_for(table, column):
        return profiled_text_patterns(conn, table, column)

    @lru_cache(maxsize=None)
    def dates_for(table, column):
        return column_uses_iso_dates(conn, table, column)

    conditions: list[Condition] = []
    grounded_pattern_bindings: list[dict[str, Any]] = []
    connector = "AND"
    bound_fact_ids: set[str] = set(excluded_fact_ids)
    predicate_tail_uncertain: set[str] = set()
    if needs_filter:
        predicate_names: tuple[str, ...] = ()
        if relation_edge:
            inspected = {entity.table: columns, joined_table: joined_table_columns}
            candidate_evidence = joined_predicate_candidates(
                conn, request, inspected,
                tuple(fact.value for fact in facts
                      if fact.kind in ("integer", "number")
                      and isinstance(fact.value, (int, float))))
            joined_predicates = resolve_join_predicate_columns(request, {
                entity.table: columns, joined_table: joined_table_columns}, client,
                candidate_evidence=candidate_evidence,
                established_outputs=tuple(f"{item.table}.{item.column}"
                                          for item in qualified_output_fields),
                established_order=(f"{qualified_order_field.table}.{qualified_order_field.column}"
                                   if qualified_order_field else None),
                relation=relation_edge, source_table=entity.table)
            record("Qualified condition columns",
                   [f"{field.table}.{field.column}"
                    for field in joined_predicates.fields], joined_predicates)
            if joined_predicates.status == "ambiguous" or not joined_predicates.fields:
                raise ValueError("The joined condition columns are unclear.")
            predicate_fields = joined_predicates.fields
        else:
            predicate_fields = ()
            value_evidence = {
                column.name: tuple(dict.fromkeys(
                    value for value in (
                        values_for(entity.table, column.name, column_affinity(column) == "numeric") +
                        patterns_for(entity.table, column.name))
                    if str(value).strip().casefold() not in _FUNCTION_WORDS or
                    re.search(r"['\"]" + re.escape(str(value).strip()) + r"['\"]",
                              request, re.I)))
                for column in columns
            }
            established_predicates = established_columns
            excluded_predicates = excluded_columns
            chosen = resolve_predicate_columns(request, entity.table, columns, client,
                ("count of source rows per group" if output_kind == "count" else
                 f"{aggregate_function}({aggregate_column})") if grouping else None,
                value_evidence,
                tuple({"id": fact.fact_id, "kind": fact.kind, "value": fact.value}
                      for fact in facts),
                established_predicates,
                excluded_predicates)
            record("Condition columns", chosen.columns, chosen)
            if not chosen.columns:
                refined = refine_predicate_candidates(request, entity.table, columns,
                    chosen.uncertain_columns or tuple(column.name for column in columns),
                    value_evidence, client)
                record("Condition role refinement", refined.columns, refined)
                chosen = refined
            predicate_tail_uncertain = set(chosen.uncertain_columns)
            if chosen.status not in {"resolved", "uncertain_tail"} and chosen.columns:
                raise ValueError("An additional predicate is ambiguous; no partial query was executed.")
            if chosen.status not in {"resolved", "uncertain_tail"} or not chosen.columns:
                if grouping and has_group_threshold:
                    # A grouped threshold can make the compact route
                    # over-call WHERE. No grounded source column means no
                    # source predicate is safe to add.
                    record("Binding guard", "ignored_ungrounded_group_filter", chosen)
                    needs_filter = False
                    predicate_names = ()
                    predicate_specs = ()
                    chosen = None
                else:
                    review = resolve_filter_need(request, entity.table, columns, client)
                    record("Source filter review", review.needed, review)
                    if review.needed:
                        raise ValueError("The condition columns are unclear.")
                    needs_filter = False
                    chosen = None
            if chosen is None:
                pass
            else:
                predicate_names = chosen.columns
        if relation_edge:
            predicate_specs = tuple((field.table, next(
                item for item in (columns if field.table == entity.table else joined_table_columns)
                if item.name == field.column)) for field in predicate_fields)
        else:
            predicate_specs = tuple((entity.table, next(item for item in columns if item.name == name))
                                    for name in predicate_names)
        for source_table, column in predicate_specs:
            name = column.name
            client.advance(Stage.COMPARISON, predicate_table=source_table, predicate_column=name, comparison=None,
                predicates=tuple((item.column, item.operator, item.value) for item in conditions))
            date_semantics = dates_for(source_table, name)
            patterns = patterns_for(source_table, name)
            hints = values_for(source_table, name, column_affinity(column) == "numeric")
            if date_semantics:
                strict = strict_date_bounds(request, tuple(fact for fact in facts
                    if fact.fact_id not in bound_fact_ids))
                if strict.status == "resolved":
                    record(f"Strict date bounds · {source_table}.{name}",
                           (strict.lower, strict.upper), strict)
                    bound_fact_ids.update(strict.fact_ids)
                    conditions.extend((
                        Condition(name, ">", strict.lower,
                                  source_table if relation_edge else None),
                        Condition(name, "<", strict.upper,
                                  source_table if relation_edge else None)))
                    continue
            if not date_semantics:
                numeric_facts = tuple(fact for fact in facts
                                      if fact.kind in ("integer", "number", "worded_number")
                                      and fact.fact_id not in bound_fact_ids)
                numeric = column_affinity(column) == "numeric"
                alternatives = resolve_alternatives(request, name, () if numeric else hints, client,
                    numeric_facts=numeric_facts if numeric else (),
                    other_columns=tuple(item.name for table, item in predicate_specs
                                        if item.name != name))
                if alternatives.jev_calls:
                    record(f"Alternative values · {source_table}.{name}",
                           alternatives.values, alternatives)
                if alternatives.values:
                    bound_fact_ids.update(alternatives.fact_ids)
                    conditions.append(Condition(
                        name, "IN", alternatives.values,
                        source_table if relation_edge else None))
                    continue
                interval = (explicit_inclusive_interval(request, numeric_facts)
                            if numeric else None)
                if interval is not None:
                    endpoints = {interval[0].fact_id, interval[1].fact_id}
                    extra_exact = [fact for fact in numeric_facts
                                   if fact.kind in {"integer", "number"}
                                   and fact.fact_id not in endpoints]
                    # A threshold or limit that is already bound is not an
                    # endpoint. When the only unbound exact numbers are the
                    # interval, ordinary code owns BETWEEN.
                    if not extra_exact:
                        lower, upper = sorted((interval[0].value, interval[1].value))
                        bounds = RangeBounds(lower, upper, "resolved",
                                             "explicit_inclusive_interval",
                                             fact_ids=tuple(fact.fact_id for fact in interval))
                        record(f"Range bounds · {source_table}.{name}",
                               (lower, upper), bounds)
                        bound_fact_ids.update(endpoints)
                        conditions.append(Condition(
                            name, "BETWEEN", (lower, upper),
                            source_table if relation_edge else None))
                        continue
            operator = resolve_predicate_operator(
                request, column, client, hints, date_semantics=date_semantics,
                pattern_hints=patterns)
            if operator.operator == "none":
                # Column roles nominate candidates. An explicit NONE comparison
                # retracts that nomination; coverage/vetting still checks omissions.
                record(f"Comparison · {source_table}.{name}", "none", operator)
                continue
            if operator.status != "resolved":
                raise ValueError("The predicate comparison is ambiguous; no partial query was executed.")
            record(f"Comparison · {source_table}.{name}", operator.operator, operator)
            client.advance(Stage.VALUE, comparison=operator.operator)
            if operator.operator in ("IS NULL", "IS NOT NULL"):
                # NULL predicates are fully specified by the selected column
                # and operator; they intentionally have no literal operand.
                conditions.append(Condition(name, operator.operator, None,
                                            source_table if relation_edge else None))
                continue
            if operator.operator == "BETWEEN":
                bounds = resolve_range_bounds(request, column.name,
                                             dates_for(source_table, column.name),
                                             client, tuple(fact for fact in facts
                                                           if fact.fact_id not in bound_fact_ids))
                record(f"Range bounds · {source_table}.{name}", (bounds.lower, bounds.upper), bounds)
                if bounds.status != "resolved":
                    raise ValueError("The requested interval bounds are unclear.")
                bound_fact_ids.update(bounds.fact_ids)
                conditions.append(Condition(name, "BETWEEN", (bounds.lower, bounds.upper),
                                            source_table if relation_edge else None))
            else:
                value = resolve_predicate_value(
                    request, column, operator.operator, hints, client, facts,
                    bound_fact_ids, date_semantics=date_semantics,
                    pattern_hints=patterns)
                record(f"Condition value · {source_table}.{name}", value.value, value)
                if value.status != "resolved":
                    # A selected column may be an output or GROUP BY key
                    # with no requested row-value restriction. Discard
                    # that unbound predicate and let final coverage catch
                    # any truly missing condition before execution.
                    record(f"Binding guard · {source_table}.{name}",
                           "ignored_unbound_predicate", value)
                    continue
                if (operator.operator in {"CONTAINS", "PREFIX", "SUFFIX"} and
                        value.source == "request_span" and
                        not any(str(value.value).casefold() in str(hint).casefold()
                                for hint in hints) and
                        not re.search(r"(?<!\w)" + re.escape(name).replace("_", r"[_\s]+") +
                                      r"(?!\w)", request, re.I)):
                    record(f"Binding guard · {source_table}.{name}",
                           "ignored_unrelated_pattern", value)
                    continue
                if not has_request_evidence(request, value.value, value.source):
                    record(f"Binding guard · {source_table}.{name}", "rejected_ungrounded_value", value)
                    continue
                if value.fact_id:
                    bound_fact_ids.add(value.fact_id)
                resolved_operator, resolved_value = operator.operator, value.value
                if value.source == "jev_grounded_observed_pattern":
                    grounded_pattern_bindings.append({
                        "table": source_table, "column": name,
                        "operator": resolved_operator, "value": resolved_value,
                        "provenance": "recurring stored annotation semantically bound by Jev",
                    })
                if (isinstance(resolved_value, int) and 1000 <= resolved_value <= 9999 and
                        dates_for(source_table, name)):
                    year = resolved_value
                    if resolved_operator == ">=":
                        resolved_value = f"{year:04d}-01-01"
                    elif resolved_operator == ">":
                        resolved_operator, resolved_value = ">=", f"{year + 1:04d}-01-01"
                    elif resolved_operator == "<":
                        resolved_value = f"{year:04d}-01-01"
                    elif resolved_operator == "<=":
                        resolved_operator, resolved_value = "<", f"{year + 1:04d}-01-01"
                    elif resolved_operator == "=":
                        resolved_operator, resolved_value = "BETWEEN", (
                            f"{year:04d}-01-01", f"{year:04d}-12-31")
                conditions.append(Condition(name, resolved_operator, resolved_value,
                                            source_table if relation_edge else None))
        explicitly_names = lambda column_name: _explicitly_names_column(request, column_name)
        # Resolve same-operand candidates independently of schema iteration order.
        # An explicitly named column must establish the predicate before an
        # unmentioned semantic lookalike can be considered as an extra condition.
        conditions = _drop_shadowed_duplicate_conditions(request, conditions)
        conditions = _prefer_explicit_condition_order(request, conditions)
        distinct_conditions: list[Condition] = []
        for condition in conditions:
            duplicate = next((prior for prior in distinct_conditions
                if prior.operator == condition.operator and prior.value == condition.value
                and explicitly_names(prior.column)
                and not explicitly_names(condition.column)), None)
            if duplicate is not None:
                explicit_binding = bool(re.search(
                    r"(?<!\w)" + re.escape(duplicate.column).replace("_", r"[_\s]+") +
                    r"(?!\w)\s+(?:(?:is|equals?)\s+)?" +
                    re.escape(str(duplicate.value)) + r"(?!\w)", request, re.I))
                review = (FilterNeed(False, "resolved", "explicit_column_operand_binding")
                          if explicit_binding else resolve_filter_need(
                              request, entity.table, columns, client,
                              relational_selector={"represented_predicate": asdict(duplicate)},
                              duplicate_candidate=asdict(condition)))
                record(f"Additional condition review · {condition.column}",
                       review.needed, review)
                if review.status != "resolved":
                    raise ValueError("An additional predicate remains ambiguous; no partial query was executed.")
                if not review.needed:
                    continue
            distinct_conditions.append(condition)
        conditions = distinct_conditions
        client.advance(Stage.FILTER, predicate_column=None, comparison=None,
            predicates=tuple((item.column, item.operator, item.value) for item in conditions))
        if not relation_edge:
            completion = resolve_predicate_completion(
                request, entity.table, small_category_hints(conn, entity.table, columns),
                tuple({"column": item.column, "operator": item.operator, "value": item.value}
                      for item in conditions), client)
            if completion.jev_calls:
                record("Predicate completeness",
                       ({"column": completion.column, "operator": completion.operator,
                         "value": completion.value}
                        if completion.column else None), completion)
            if (completion.column is not None and completion.value is not None and
                    completion.operator is not None):
                conditions.append(Condition(completion.column, completion.operator,
                                            completion.value))
            unresolved_tail = predicate_tail_uncertain - {
                item.column for item in conditions}

            def tail_has_grounded_operand(name: str) -> bool:
                if explicitly_names(name):
                    return True
                column = next(item for item in columns if item.name == name)
                related = mechanically_related_values(
                    request, tuple(str(value) for value in values_for(entity.table, name)),
                    column_name=name)
                if any(evidence.get("kind") in {
                        "exact_request_phrase", "request_initialism",
                        "compact_observed_code_prefix",
                        "compact_observed_single_code_prefix"}
                       for _, evidence in related):
                    return True
                if dates_for(entity.table, name):
                    return any(fact.kind in {"date", "integer", "worded_number"}
                               for fact in facts)
                if column_affinity(column) == "numeric":
                    return any(fact.kind in {"integer", "number", "worded_number"}
                               and fact.fact_id not in bound_fact_ids for fact in facts)
                return False

            unresolved_tail = {name for name in unresolved_tail
                if (name not in output_fields or _has_exact_value_evidence(
                    request, tuple(str(value) for value in values_for(
                        entity.table, name))))
                and tail_has_grounded_operand(name)}
            if unresolved_tail:
                review = resolve_filter_need(
                    request, entity.table, columns, client,
                    relational_selector={"represented_predicates": tuple(
                        {"column": item.column, "operator": item.operator,
                         "value": item.value} for item in conditions),
                        "unresolved_candidate_columns": tuple(sorted(unresolved_tail))},
                    literal_facts=tuple({"id": fact.fact_id, "kind": fact.kind,
                                         "value": fact.value} for fact in facts))
                record("Predicate tail review", review.needed, review)
                if review.needed:
                    raise ValueError(
                        "An additional predicate remains ambiguous; no partial query was executed.")
        predicate_names = tuple(condition.column for condition in conditions)
        if needs_filter and not conditions:
            review = resolve_filter_need(request, entity.table, columns, client)
            record("Source filter review", review.needed, review)
            if review.needed:
                raise ValueError("No source-row predicate was grounded in the request evidence.")
            needs_filter = False
        client.advance(Stage.LOGIC,
            predicates=tuple((item.column, item.operator, item.value) for item in conditions))
        if len(predicate_names) > 1:
            logic = resolve_condition_logic(
                request, predicate_names, client,
                tuple({"column": item.column, "operator": item.operator,
                       "value": item.value} for item in conditions))
            record("Condition logic", logic.connector, logic)
            if logic.status != "resolved":
                raise ValueError("The requested Boolean condition is not yet supported.")
            connector = logic.connector
    elif not relation_edge:
        completion = resolve_predicate_completion(
            request, entity.table, small_category_hints(conn, entity.table, columns),
            (), client)
        if completion.jev_calls:
            record("Predicate completeness",
                   ({"column": completion.column, "operator": completion.operator,
                     "value": completion.value}
                    if completion.column else None), completion)
        if (completion.column is not None and completion.value is not None and
                completion.operator is not None):
            conditions.append(Condition(completion.column, completion.operator,
                                        completion.value))
    return ConditionPlan(tuple(conditions), connector, tuple(grounded_pattern_bindings))


def run_read(db_path: str | Path, request: str, client: JevClient,
             on_step: Callable[[dict[str, Any]], None] | None = None,
             on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Invoke only semantic branches routed for this request."""
    steps: list[dict[str, Any]] = []
    calls = input_tokens = output_tokens = 0
    resolved: dict[str, Any] = {}

    def publish_decision(name: str, selected: Any) -> None:
        resolved[name] = selected
        if on_event:
            on_event({"kind": "program", "program": {"resolving": True,
                      "decisions": dict(resolved)}})

    def record(name: str, selected: Any, result: Any) -> None:
        nonlocal calls, input_tokens, output_tokens
        publish_decision(name, selected)
        calls += result.jev_calls
        input_tokens += result.input_tokens
        output_tokens += result.output_tokens
        for index, evidence in enumerate(result.trace or (None,)):
            step = {"name": name if index == 0 else f"{name} refinement",
                    "selected": selected,
                    "source": result.source if hasattr(result, "source") else "jev_choice",
                    "usage": evidence}
            steps.append(step)
            if on_step:
                on_step(step)

    path = Path(db_path).resolve()
    if on_event:
        on_event({"kind": "phase", "phase": "inspect"})
    with connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        names = table_names(conn)
        # Schema/profile facts are immutable for this read-only connection.
        # Inspect once and reuse them across progressively narrowed branches.
        schema_columns = {name: columns_for(conn, name) for name in names}
        numeric_profiles: dict[tuple[str, str], Any] = {}

        def profiled(table: str, column: str):
            key = (table, column)
            if key not in numeric_profiles:
                numeric_profiles[key] = numeric_profile(conn, table, column)
            return numeric_profiles[key]

        table_metadata = {
            name: [{"name": column.name, "declared_type": column.declared_type}
                   for column in schema_columns[name]]
            for name in names
        }
        if on_event:
            on_event({"kind": "phase", "phase": "resolve"})
        facts = extract_facts(request)
        if any(fact.kind == "invalid_date" for fact in facts):
            raise ValueError("The request contains an invalid calendar date; no query was executed.")
        named_source = explicitly_named_source(request, names)
        entity = (Entity(named_source, "resolved", "explicit_schema_name")
                  if named_source else
                  resolve_entity(request, names, client, table_metadata))
        record("Source", entity.table, entity)
        if (entity.status != "resolved" or entity.table is None) and len(names) > 1:
            relationship_context: dict[str, Any] = {}
            for candidate_table in names:
                related = []
                for edge in direct_relations(conn, candidate_table):
                    other = edge.other_table(candidate_table)
                    related.append({
                        "table": other,
                        "columns": [column.name for column in schema_columns[other]],
                        "foreign_key": {
                            "table": edge.child_table,
                            "column": edge.child_column,
                            "references": [edge.parent_table, edge.parent_column],
                        },
                    })
                if related:
                    relationship_context[candidate_table] = related
            if relationship_context:
                anchor = resolve_entity(
                    request, names, client, table_metadata, relationship_mode=True,
                    relationship_context=relationship_context)
                record("Source relation anchor", anchor.table, anchor)
                if anchor.status == "resolved" and anchor.table is not None:
                    entity = anchor
        if entity.status != "resolved" or entity.table is None:
            available = ", ".join(names)
            raise ValueError(
                "No single source table or direct-relationship anchor in the selected database "
                "matches this request. "
                f"Available tables: {available}. Choose the database containing the requested data "
                "or name one of these tables explicitly.")
        columns = schema_columns[entity.table]
        literal_state = tuple((fact.fact_id, fact.kind, fact.value) for fact in facts)
        client = DecisionClient(client, ReadState(entity.table, literal_facts=literal_state))
        intent = route_intent(request, client, source_table=entity.table, columns=columns, facts=facts)
        calls += intent.jev_calls
        input_tokens += intent.input_tokens
        output_tokens += intent.output_tokens
        route_step = {"name": "Route", "selected": ", ".join(intent.relevant_branches),
                      "source": "jev_semantic_route", "usage": {
                          "purpose": "route only needed semantic skills",
                          "selected": {"output": intent.output, "quantity": intent.quantity,
                                       "filters": intent.filters, "ordering": intent.ordering,
                                       "distinct": intent.distinct, "grouping": intent.grouping,
                                       "relationship": intent.relationship,
                                       "having": intent.having,
                                       "expression": intent.expression,
                                       "windowing": intent.windowing},
                          "scores": intent.scores, "probabilities": intent.probabilities,
                          "confidence": intent.confidence,
                          "input_tokens": intent.input_tokens, "output_tokens": intent.output_tokens,
                          "elapsed_ms": intent.elapsed_ms}}
        publish_decision("Route", route_step["usage"]["selected"])
        steps.append(route_step)
        if on_step:
            on_step(route_step)
        if intent.expression and intent.output != "other":
            raise ValueError("This derived expression is not supported; no partial query was executed.")
        output_kind, grouping = intent.output, intent.grouping
        client.advance(Stage.SHAPE, output=output_kind)
        per_group_extremum = None
        global_row_extremum = False
        # The route is a cheap fan-out hint, not a semantic veto.  Strong
        # row-per-group wording gets the specialized bounded shape resolver even
        # when the broad route calls it ordinary rows (or mistakes "first" for a
        # scalar aggregate).  The specialized resolver still owns the actual
        # group, selector column, direction, and supported/unsupported decision.
        if should_probe_per_group_shape(
                request, output_kind, grouping_hint=intent.grouping,
                windowing_hint=intent.windowing):
            iso_date_columns = tuple(
                column.name for column in columns
                if column_uses_iso_dates(conn, entity.table, column.name))
            grouped_shape = resolve_per_group_extremum(
                request, entity.table, columns, client,
                iso_date_columns=iso_date_columns,
                aggregate_route_hint=output_kind == "other" and grouping)
            record("Grouped result shape", grouped_shape.shape, grouped_shape)
            if grouped_shape.status != "resolved":
                raise ValueError(
                    "This request requires unsupported window/ranking semantics; "
                    "no partial query was executed.")
            if grouped_shape.shape in {"per_group_extremum", "per_group_representative"}:
                per_group_extremum = grouped_shape
                # These are row-selection predicates, not aggregate result shapes.
                # Correct a coarse output-route miss here instead of preserving it.
                grouping = False
                explicit_result_fields = tuple(column.name for column in columns
                    if column.name not in {grouped_shape.group_column,
                                           grouped_shape.extremum_column}
                    and not column.primary_key
                    and re.search(r"(?<!\w)" + re.escape(column.name).replace(
                        "_", r"[_\s]+") + r"(?!\w)", request, re.I))
                output_kind = "fields" if explicit_result_fields else "rows"
            elif grouped_shape.shape in {"ordinary_rows", "global_extremum"}:
                grouping = False
                has_group_threshold = False
                if grouped_shape.shape == "global_extremum":
                    global_row_extremum = True
                    needs_ordering = True
            elif grouped_shape.shape == "grouped_count":
                output_kind = "count"
            elif grouped_shape.shape == "grouped_aggregate":
                output_kind = "other"
        distinct = intent.distinct if output_kind in ("fields", "count") else False
        if global_row_extremum:
            distinct = False
        has_group_threshold = intent.having and grouping
        if output_kind == "count" and (grouping or distinct):
            shape = resolve_count_shape(request, tuple(column.name for column in columns), client)
            record("Count shape", shape.shape, shape)
            if shape.shape == "distinct":
                grouping, distinct, has_group_threshold = False, True, False
            elif shape.shape == "grouped":
                grouping, distinct = True, False
            else:
                grouping, distinct, has_group_threshold = False, False, False
        # "First/last in each group" describes the correlated extremum itself;
        # it does not imply a second global ordering operation.
        needs_ordering = intent.ordering or (
            global_row_extremum and per_group_extremum is None)
        if output_kind == "other":
            aggregate_shape = resolve_aggregate_shape(request, entity.table, columns, client)
            record("Aggregate shape", aggregate_shape.shape, aggregate_shape)
            if aggregate_shape.status != "resolved":
                raise ValueError("This computed result requires unsupported nested aggregation.")
            if aggregate_shape.shape == "row_extremum":
                output_kind, grouping, global_row_extremum = "fields", False, True
                needs_ordering = True
            else:
                grouping = aggregate_shape.shape == "grouped"
                if not grouping:
                    has_group_threshold = False
        if grouping and output_kind == "fields":
            reachable = {entity.table: columns}
            for edge in direct_relations(conn, entity.table):
                neighbor = edge.other_table(entity.table)
                reachable[neighbor] = schema_columns[neighbor]
            field_shape = resolve_field_shape(
                request, entity.table, reachable, distinct, client,
                stored_evidence={column.name: targeted_values(
                    conn, entity.table, column.name, request,
                    include_extrema=column_affinity(column) == "numeric")
                    for column in columns if not column.primary_key})
            record("Field result shape", field_shape.shape, field_shape)
            if field_shape.status != "resolved":
                raise ValueError("The requested field result shape is unclear.")
            distinct = field_shape.distinct
            if field_shape.shape == "rows":
                grouping = False
                has_group_threshold = False
            elif field_shape.shape == "global_extremum":
                grouping = False
                has_group_threshold = False
                global_row_extremum = True
                needs_ordering = True
            else:
                # Group keys already identify distinct result groups. A
                # separate SELECT DISTINCT has no meaning for an aggregate.
                distinct = False
        if grouping and output_kind == "fields":
            measure = resolve_group_measure(request, entity.table,
                                            tuple(column.name for column in columns), client)
            record("Group measure", measure.kind, measure)
            if measure.status != "resolved":
                raise ValueError("The grouped measure is unclear; no partial query was executed.")
            output_kind = "count" if measure.kind == "count" else "other"
            has_group_threshold = has_group_threshold or measure.qualifies_groups
        # A scalar aggregate produces exactly one row. Router votes about ordering
        # cannot change that typed shape, so do not let noisy ordering hints send
        # the request into a row-ordering branch or reject an otherwise valid scalar.
        if _is_scalar_aggregate_result(output_kind, grouping):
            needs_ordering = False
        relation_edge: Relation | None = None
        joined_table: str | None = None
        joined_table_columns: tuple[Any, ...] = ()
        relationship_scope = "local_only"
        client.advance(Stage.RELATION, output=output_kind)
        edges = direct_relations(conn, entity.table) if len(names) > 1 else ()
        relation_candidates = {edge.other_table(entity.table):
                               schema_columns[edge.other_table(entity.table)] for edge in edges}
        neighbor_evidence = bool(relation_candidates and _neighbor_evidence(
            request, entity.table, columns, relation_candidates))
        explicit_local_fk = any(
            column.name.casefold().endswith("_id") and re.search(
                r"(?<!\w)" + re.escape(column.name).replace("_", r"[_\s]+") + r"(?!\w)",
                request, re.I) for column in columns)
        grounded_relation = neighbor_evidence and not explicit_local_fk and any(
            re.search(r"(?<!\w)(?:" + re.escape(table.replace("_", " ")) +
                      (r"|" + re.escape(table[:-1].replace("_", " "))
                       if table.endswith("s") else "") + r")(?!\w)",
                      request, re.I)
            for table in relation_candidates)
        neighbor_measure = bool(relation_candidates) and (
            grounded_relation or _count_depends_on_unnamed_neighbor(
                output_kind, neighbor_evidence, grounded_relation, explicit_local_fk))
        if neighbor_measure and output_kind in {"count", "other"} and relation_candidates:
            # Words such as "count" may name a stored numeric attribute rather
            # than request SQL COUNT(). Give inspected local + adjacent columns
            # one bounded chance to disambiguate before rejecting aggregate-over-join.
            inspected = {entity.table: columns, **relation_candidates}
            stored = resolve_stored_output(request, inspected, client)
            record("Stored output check", stored.stored, stored)
            if stored.stored:
                output_kind, grouping, has_group_threshold = "fields", False, False
                global_row_extremum = False
                distinct = intent.distinct
                needs_ordering = intent.ordering
                client.advance(Stage.RELATION, output=output_kind)
        if neighbor_measure and output_kind in {"count", "other"}:
            raise ValueError(
                "This grouped result needs related-table output beyond the supported aggregate IR; "
                "no partial query was executed.")
        relation_check = (grounded_relation or intent.relationship or
                          (output_kind in {"fields", "rows"} and neighbor_evidence))
        if len(names) > 1 and relation_check:
            if not edges:
                edges = direct_relations(conn, entity.table)
            relationship_context = {
                edge.other_table(entity.table): {
                    "columns": tuple(column.name for column in columns_for(
                        conn, edge.other_table(entity.table))),
                    "foreign_key": {
                        "child_table": edge.child_table,
                        "child_column": edge.child_column,
                        "parent_table": edge.parent_table,
                        "parent_column": edge.parent_column,
                    },
                }
                for edge in edges
            }
            local_value_evidence = {
                column.name: tuple(dict.fromkeys(
                    targeted_values(conn, entity.table, column.name, request) +
                    profiled_text_patterns(conn, entity.table, column.name)))
                for column in columns
            }
            local_value_evidence = {name: values for name, values in
                                    local_value_evidence.items() if values}
            relationship = (RelationshipNeed(True, "resolved", "grounded_neighbor_schema")
                            if grounded_relation else resolve_relationship_need(
                                request, entity.table, columns, client,
                                related_schema=relationship_context,
                                output_mode=("all source columns" if output_kind == "rows" else
                                             "selected fields" if output_kind == "fields" else
                                             "computed result"),
                                local_value_evidence=local_value_evidence))
            record("Relationship check", relationship.needs_other_table, relationship)
            if relationship.needs_other_table:
                relationship_scope = "direct_relation"
                neighbors = {edge.other_table(entity.table):
                             tuple(column.name for column in schema_columns[edge.other_table(entity.table)])
                             for edge in edges}
                if output_kind == "count" and neighbors:
                    inspected = {entity.table: columns,
                                 **{name: schema_columns[name] for name in neighbors}}
                    stored = resolve_stored_output(request, inspected, client)
                    record("Stored output check", stored.stored, stored)
                    if stored.stored:
                        output_kind, grouping = "fields", False
                        needs_ordering = intent.ordering
                reanchored_scalar = False
                if (_can_reanchor_scalar_aggregate(
                        output_kind, grouping, has_group_threshold, intent.filters) and neighbors):
                    related = resolve_related_table(request, entity.table, edges, client,
                                                   related_columns=neighbors)
                    target_table = (related.relation.other_table(entity.table)
                                    if related.relation else None)
                    record("Direct relation", target_table, related)
                    if related.status == "resolved" and related.relation is not None:
                        previous_table = entity.table
                        entity = Entity(target_table, "resolved",
                                        "scalar_aggregate_metric_source")
                        columns = schema_columns[target_table]
                        relationship_scope = "local_only"
                        # The original named entity contributes no output/filter;
                        # the directly related metric-owning table can therefore
                        # become the row grain without changing semantics.
                        transport = client.client if isinstance(client, DecisionClient) else client
                        client = DecisionClient(transport, ReadState(
                            target_table, literal_facts=literal_state, output=output_kind))
                        step = {"name": "Aggregate source refinement",
                                "selected": target_table,
                                "source": "deterministic_relation_reanchor",
                                "usage": {"from": previous_table,
                                          "reason": "scalar aggregate metric lives in a direct relation and the original source contributes no filter"}}
                        steps.append(step)
                        publish_decision(step["name"], step["selected"])
                        if on_step:
                            on_step(step)
                        reanchored_scalar = True
                if not reanchored_scalar:
                    if (output_kind not in {"fields", "rows"} or grouping or
                            has_group_threshold):
                        raise ValueError("This multi-table request needs a join capability beyond the proven direct-field path.")
                    related = resolve_related_table(request, entity.table, edges, client,
                                                   related_columns=neighbors)
                    record("Direct relation", related.relation.other_table(entity.table)
                           if related.relation else None, related)
                    if related.status != "resolved" or related.relation is None:
                        raise ValueError("No clear direct foreign-key relationship can satisfy this request.")
                    relation_edge = related.relation
                    joined_table = relation_edge.other_table(entity.table)
                    joined_table_columns = schema_columns[joined_table]
        if (relation_edge and joined_table and
                should_probe_relation_absence(request)):
            absence = resolve_relation_absence(
                request, entity.table, joined_table, relation_edge,
                columns, joined_table_columns, client)
            record("Related-row absence", absence.mode, absence)
            if absence.status != "resolved" or absence.mode == "anti_join_absence":
                raise ValueError(
                    "This request requires unsupported anti-join/NOT EXISTS semantics; "
                    "no partial query was executed.")
        client.advance(Stage.FILTER, relation=joined_table,
            per_group=(per_group_extremum.group_column, per_group_extremum.extremum_column,
                       per_group_extremum.function) if per_group_extremum else None)
        needs_filter = intent.filters
        if per_group_extremum:
            filter_check = resolve_filter_need(
                request, entity.table, columns, client,
                relational_selector={
                    "kind": "per_group_extremum",
                    "group_column": per_group_extremum.group_column,
                    "extremum_column": per_group_extremum.extremum_column,
                    "function": per_group_extremum.function,
                })
            record("Source filter check", filter_check.needed, filter_check)
            needs_filter = filter_check.needed
        elif not needs_filter and .25 <= intent.scores["filters"] < .6:
            filter_check = resolve_filter_need(request, entity.table, columns, client,
                                               group_measure_filter=has_group_threshold,
                                               category_hints=(small_category_hints(conn, entity.table, columns)
                                                               if has_group_threshold else None))
            record("Source filter check", filter_check.needed, filter_check)
            needs_filter = filter_check.needed
        if not needs_filter and not grouping:
            exact_source_values = {
                column.name: matching for column in columns
                if column_affinity(column) != "numeric"
                if (matching := tuple(value for value in targeted_values(
                    conn, entity.table, column.name, request)
                    if str(value).strip().casefold() not in _FUNCTION_WORDS and
                    _has_exact_value_evidence(request, (value,))))
            }
            if exact_source_values:
                filter_check = resolve_filter_need(
                    request, entity.table, columns, client,
                    category_hints=exact_source_values)
                record("Grounded source filter check", filter_check.needed, filter_check)
                needs_filter = filter_check.needed
        if not needs_filter and relation_edge and joined_table:
            category_evidence = _matching_category_evidence(
                request, small_category_hints(conn, joined_table, joined_table_columns))
            joined_candidates = joined_predicate_candidates(
                conn, request, {joined_table: joined_table_columns})
            related_values = {
                label.split(".", 1)[1]: tuple(
                    str(item["value"]) for item in detail.get("related_stored_values", ()))
                for label, detail in joined_candidates.items()
                if detail.get("related_stored_values")}
            category_evidence = {**related_values, **category_evidence}
            if category_evidence:
                candidate_columns = tuple(column for column in joined_table_columns
                                          if column.name in category_evidence)
                filter_check = resolve_filter_need(
                    request, joined_table, candidate_columns, client,
                    candidate_value_evidence=category_evidence)
                record("Joined category filter check", filter_check.needed, filter_check)
                needs_filter = filter_check.needed
        valid_columns = tuple(column.name for column in columns)
        chosen_fields: tuple[str, ...] = ()
        qualified_fields: tuple[QualifiedField, ...] = ()
        aggregate_function = aggregate_column = None
        group_column = extra_group_column = None
        client.advance(Stage.OUTPUT, output=output_kind)
        if grouping:
            group = resolve_group_key(request, entity.table, columns, client)
            record("Group key", group.column, group)
            if group.status != "resolved" or group.column is None:
                raise ValueError("The grouping key is unclear.")
            group_column = group.column
            if output_kind == "count" and len(columns) > 2:
                extra = resolve_extra_group_key(request, entity.table, group_column, columns, client)
                record("Additional group key", extra.column, extra)
                extra_group_column = extra.column
        if output_kind == "fields" or (output_kind == "count" and distinct):
            if relation_edge:
                joined_fields = resolve_join_fields(request, {
                    entity.table: columns, joined_table: joined_table_columns}, client,
                    relation=relation_edge)
                record("Qualified output fields", [f"{field.table}.{field.column}"
                                                   for field in joined_fields.fields], joined_fields)
                if joined_fields.status != "resolved":
                    raise ValueError("The requested joined output columns are unclear.")
                qualified_fields = joined_fields.fields
            else:
                fields = (resolve_distinct_target(request, entity.table, columns, client)
                          if output_kind == "count" else
                          resolve_fields(request, entity.table, columns, client))
                record("Output fields", fields.columns, fields)
                if fields.status != "resolved":
                    raise ValueError("The requested output columns are unclear.")
                chosen_fields = fields.columns
                if output_kind == "count" and fields.source == "jev_row_count":
                    distinct = False
                if output_kind == "fields" and fields.all_columns:
                    output_kind = "rows"
                if output_kind == "count" and distinct and len(chosen_fields) != 1:
                    raise ValueError("Count distinct requires exactly one resolved column.")
        if output_kind == "other":
            aggregate = resolve_aggregate(request, entity.table, columns, client,
                                          group_key=group_column if grouping else None)
            record("Aggregate", f"{aggregate.function}({aggregate.column})", aggregate)
            if aggregate.status != "resolved" or not aggregate.column or not aggregate.function:
                raise ValueError("This computed result is not a supported scalar aggregate.")
            selected_column = next(item for item in columns if item.name == aggregate.column)
            if aggregate.function in ("AVG", "SUM") and column_affinity(selected_column) != "numeric":
                profile = profiled(entity.table, aggregate.column)
                detail = {"numeric_values": profile.numeric_values,
                          "nonnumeric_values": profile.nonnumeric_values,
                          "sqlite_nonnumeric_text_counts_as_zero": bool(profile.nonnumeric_values)}
                publish_decision("Aggregate source profile", detail)
                step = {"name": "Aggregate source profile", "selected": detail,
                        "source": "sqlite_inspection", "usage": detail}
                steps.append(step)
                if on_step:
                    on_step(step)
                if not profile.compatible and not column_uses_iso_dates(
                        conn, entity.table, aggregate.column):
                    raise ValueError("The selected aggregate needs predominantly numeric source values.")
            aggregate_function, aggregate_column = aggregate.function, aggregate.column
        round_places = None
        rounding_fact_id = None
        if intent.expression:
            measure = f"{aggregate_function}({aggregate_column})"
            rounding = resolve_rounding(request, measure, client)
            record("Rounding", rounding.places, rounding)
            if rounding.status != "resolved" or rounding.places is None:
                raise ValueError("This derived expression is not supported; no partial query was executed.")
            round_places = rounding.places
            rounding_fact_id = rounding.fact_id
        early_quantity = None
        if (_can_apply_row_limit(output_kind, grouping) and
                intent.quantity in ("numeric", "worded") and per_group_extremum is None):
            client.advance(Stage.QUANTITY)
            early_quantity = resolve_quantity(request, intent.quantity, client, facts,
                                               other_numeric_roles=bool(intent.filters or intent.having))
            record("Row limit", early_quantity.value, early_quantity)
            if early_quantity.status != "bounded":
                raise ValueError("The requested output row count is unclear.")
            client.advance(Stage.SHAPE, result_limit=early_quantity.value)
        quantity_fact_ids = ((early_quantity.fact_id,)
                             if early_quantity and early_quantity.fact_id else ())
        if grouping:
            measure = ("count of source rows per group" if output_kind == "count" else
                       f"{aggregate_function} of {aggregate_column} per group")
            clauses = resolve_group_clauses(request, group_column, measure, client,
                                           excluded_fact_ids=quantity_fact_ids)
            record("Group clauses", {"having": clauses.having,
                                      "ordering": clauses.ordering}, clauses)
            # The schema/measure-aware decision supersedes the coarse router.
            # An output cardinality literal is never itself proof of HAVING.
            has_group_threshold = clauses.having
            needs_ordering = clauses.ordering
        having_operator = having_value = None
        having_aggregate_function = having_aggregate_column = None
        excluded_fact_ids: tuple[str, ...] = quantity_fact_ids + ((rounding_fact_id,)
                                               if rounding_fact_id else ())
        if grouping and has_group_threshold:
            output_measure = ("count of source rows" if output_kind == "count" else
                              f"{aggregate_function} of {aggregate_column}")
            having_measure = resolve_having_measure(
                request, group_column, output_measure, client)
            record("Grouped threshold measure", having_measure.kind, having_measure)
            if having_measure.status != "resolved":
                raise ValueError(
                    "The grouped threshold requires an unsupported aggregate measure.")
            has_group_threshold = having_measure.kind in {"output_measure", "row_count"}
            if having_measure.kind == "row_count":
                having_aggregate_function = "COUNT"
                having_aggregate_column = None
        if has_group_threshold:
            measure = ("count of source rows per group"
                       if having_aggregate_function == "COUNT" or output_kind == "count" else
                       f"{aggregate_function} of {aggregate_column} per group")
            having = resolve_having(request, group_column, measure, client)
            record("Grouped threshold", f"{having.operator} {having.value}", having)
            if having.status != "resolved" or having.operator is None or having.value is None:
                raise ValueError("The grouped threshold is unclear.")
            having_operator, having_value = having.operator, having.value
            if having.fact_id:
                # The grouped threshold literal is already a typed fact.
                # Later WHERE binding must not reuse it as a range endpoint
                # or an alternative value.  Preserve any transformation-owned
                # literal (for example ROUND(..., 2)) as independently bound.
                excluded_fact_ids = tuple(dict.fromkeys(
                    excluded_fact_ids + (having.fact_id,)))
        literal_filter_evidence = _literal_facts_require_source_filter(
            request, facts, output_kind=output_kind, grouping=grouping,
            has_group_threshold=has_group_threshold,
            having_value=having_value, round_places=round_places)
        if literal_filter_evidence and not needs_filter:
            needs_filter = True
            step = {
                "name": "Literal filter evidence",
                "selected": True,
                "source": "deterministic_literal_role",
                "usage": {
                    "reason": (
                        "exact request literals require source-row binding for this typed "
                        "result shape; the coarse filter route is advisory"),
                    "facts": [{"id": fact.fact_id, "kind": fact.kind,
                               "value": fact.value} for fact in facts],
                },
            }
            steps.append(step)
            publish_decision(step["name"], step["selected"])
            if on_step:
                on_step(step)
        if grouping:
            # Decide WHERE only after the group key and measure are typed. The
            # router is a hint; exact request literals remain grounded evidence
            # and cannot disappear merely because the compact route missed them.
            measure = ("count of source rows per group" if output_kind == "count" else
                       f"{aggregate_function} of {aggregate_column} per group")
            client.advance(
                Stage.FILTER,
                aggregate=((aggregate_function, aggregate_column)
                           if aggregate_function else None),
                groups=tuple(key for key in (group_column, extra_group_column) if key))
            exact_operands = tuple({"id": fact.fact_id, "kind": fact.kind,
                                    "value": fact.value} for fact in facts)
            group_value_evidence: dict[str, tuple[str, ...]] = {}
            if group_column:
                retrieved = targeted_values(
                    conn, entity.table, group_column, request)
                related = mechanically_related_values(
                    request, retrieved, column_name=group_column)
                exact_related = tuple(
                    (value, evidence) for value, evidence in related
                    if evidence.get("kind") == "exact_request_phrase")
                if exact_related:
                    group_value_evidence[group_column] = tuple(
                        value for value, _ in exact_related)
            if literal_filter_evidence or (intent.filters and group_value_evidence):
                # A mechanically explicit interval (or another already-proven
                # literal role) establishes a focused WHERE binding job.  The
                # broad quantity/filter route cannot erase that evidence.
                needs_filter = True
            else:
                filter_check = resolve_filter_need(
                    request, entity.table, columns, client,
                    group_measure_filter=has_group_threshold,
                    category_hints=small_category_hints(
                        conn, entity.table, columns),
                    literal_facts=exact_operands,
                    established_groups=tuple(key for key in
                                             (group_column, extra_group_column) if key),
                    established_measure=measure,
                    candidate_value_evidence=group_value_evidence)
                record("Source filter check", filter_check.needed, filter_check)
                needs_filter = filter_check.needed
        client.advance(Stage.ORDER, projection=chosen_fields or tuple(
            f"{field.table}.{field.column}" for field in qualified_fields),
            aggregate=(aggregate_function, aggregate_column) if aggregate_function else None,
            groups=tuple(key for key in (group_column, extra_group_column) if key))
        implicit_top_one = False
        # The compact route may correctly identify a field/join request while missing
        # that a singular extremum necessarily implies both ordering and LIMIT 1.
        # Refine global cardinality before choosing an ORDER BY; no schema/domain
        # vocabulary is hard-coded here.
        if (output_kind == "fields" and not grouping and intent.quantity == "none"
                and not global_row_extremum
                and not needs_ordering):
            cardinality = resolve_result_cardinality(request, None, client)
            record("Result cardinality", cardinality.mode, cardinality)
            if cardinality.mode == "top_one":
                implicit_top_one = True
                needs_ordering = True

        order_column = order_direction = order_table = None
        secondary_order_column = secondary_order_direction = None
        group_order_target = group_order_direction = group_tie_direction = None
        group_secondary_target = group_secondary_direction = None
        if needs_ordering and group_column:
            measure = ("count of rows" if output_kind == "count" else
                       f"{aggregate_function} of {aggregate_column}")
            grouped_order = resolve_group_ordering(request, group_column, measure, client)
            record("Grouped ordering", f"{grouped_order.target} {grouped_order.direction}", grouped_order)
            if grouped_order.status != "resolved":
                raise ValueError("The grouped ordering is unclear.")
            group_order_target = grouped_order.target
            group_order_direction = grouped_order.direction
            group_tie_direction = grouped_order.tie_key_direction
            group_secondary_target = grouped_order.secondary_target
            group_secondary_direction = grouped_order.secondary_direction
        elif needs_ordering and relation_edge:
            ordering = resolve_join_ordering(request, {
                entity.table: columns, joined_table: joined_table_columns}, client)
            record("Qualified ordering", f"{ordering.field.table}.{ordering.field.column} {ordering.direction}"
                   if ordering.field else None, ordering)
            if ordering.status != "resolved" or ordering.field is None:
                raise ValueError("The joined ordering is unclear.")
            order_table, order_column, order_direction = (
                ordering.field.table, ordering.field.column, ordering.direction)
        elif needs_ordering:
            ordering = resolve_ordering(request, entity.table, columns, client)
            record("Ordering", f"{ordering.column} {ordering.direction}", ordering)
            if ordering.status != "resolved":
                raise ValueError("The ordering is unclear.")
            order_column, order_direction = ordering.column, ordering.direction
            secondary_order_column = ordering.secondary_column
            secondary_order_direction = ordering.secondary_direction
        client.advance(Stage.FILTER, ranking=tuple(
            (column, direction) for column, direction in (
                (order_column, order_direction), (secondary_order_column, secondary_order_direction))
            if column is not None))
        condition_plan = plan_conditions(
            conn, request, entity, columns, client, facts, record, profiled,
            needs_filter=needs_filter, output_kind=output_kind,
            output_fields=chosen_fields,
            grouping=grouping, aggregate_function=aggregate_function,
            aggregate_column=aggregate_column,
            has_group_threshold=has_group_threshold,
            relation_edge=relation_edge, joined_table=joined_table,
            joined_table_columns=joined_table_columns,
            qualified_output_fields=qualified_fields,
            qualified_order_field=(QualifiedField(order_table or entity.table,
                                                  order_column) if relation_edge and
                                   order_column else None),
            excluded_fact_ids=excluded_fact_ids)
        conditions = list(condition_plan.conditions)
        connector = condition_plan.connector
        grounded_pattern_bindings = list(condition_plan.grounded_pattern_bindings)
        client.advance(Stage.QUANTITY, predicate_column=None, comparison=None,
            predicates=tuple((item.column, item.operator, item.value) for item in conditions))
        limit = None
        if (_can_apply_row_limit(output_kind, grouping) and
                intent.quantity in ("numeric", "worded") and
                per_group_extremum is None):
            quantity = early_quantity
            if quantity.status != "bounded":
                raise ValueError("The requested output row count is unclear.")
            limit = quantity.value
        elif implicit_top_one:
            limit = 1
        elif (not global_row_extremum and intent.quantity == "none" and
              (group_order_target or order_column)):
            ordering_context = (
                {"kind": "grouped", "target": group_order_target,
                 "direction": group_order_direction, "group_key": group_column}
                if group_order_target else
                {"kind": "rows", "column": order_column,
                 "direction": order_direction, "table": order_table or entity.table}
            )
            cardinality = resolve_result_cardinality(request, ordering_context, client)
            record("Result cardinality", cardinality.mode, cardinality)
            if cardinality.mode == "top_one":
                limit = 1
        if global_row_extremum and limit is None:
            cardinality = resolve_result_cardinality(
                request, {"column": order_column, "direction": order_direction,
                          "table": order_table or entity.table}, client,
                tie_preserving_extremum=True)
            record("Extremum cardinality", cardinality.mode, cardinality)
            if cardinality.status != "resolved":
                raise ValueError("The requested extremum cardinality is unclear; no partial query was executed.")
            if cardinality.mode == "top_one":
                limit = 1
        if global_row_extremum and not _keep_global_extremum_for_limit(
                global_row_extremum, limit):
            global_row_extremum = False
            step = {"name": "Global extremum refinement",
                    "selected": "ranked_top_n",
                    "source": "resolved_quantity",
                    "usage": {"limit": limit,
                              "reason": "an explicit top-N greater than one is ranking + LIMIT, not equality to one global MIN/MAX"}}
            steps.append(step)
            publish_decision(step["name"], step["selected"])
            if on_step:
                on_step(step)
        if (limit == 1 and output_kind == "fields" and not group_column
                and order_column is None):
            client.advance(Stage.ORDER)
            order_need = resolve_single_row_order_need(request, client)
            record("Single-row order check", order_need.needed, order_need)
            if order_need.needed:
                if relation_edge:
                    ordering = resolve_join_ordering(request, {
                        entity.table: columns, joined_table: joined_table_columns}, client)
                    record("Qualified ordering", f"{ordering.field.table}.{ordering.field.column} {ordering.direction}"
                           if ordering.field else None, ordering)
                    if ordering.status != "resolved" or ordering.field is None:
                        raise ValueError("The requested single-row ranking is unclear.")
                    order_table, order_column, order_direction = (
                        ordering.field.table, ordering.field.column, ordering.direction)
                else:
                    ordering = resolve_ordering(request, entity.table, columns, client)
                    record("Ordering", f"{ordering.column} {ordering.direction}", ordering)
                    if ordering.status != "resolved":
                        raise ValueError("The requested single-row ranking is unclear.")
                    order_column, order_direction = ordering.column, ordering.direction
                    secondary_order_column = ordering.secondary_column
                    secondary_order_direction = ordering.secondary_direction
        query = SelectQuery(entity.table, "aggregate" if output_kind == "other" else output_kind,
                            chosen_fields,
                            tuple(conditions), connector, order_column, order_direction,
                            limit, distinct, aggregate_function, aggregate_column,
                            group_column, group_order_target,
                            group_order_direction, group_tie_direction,
                            having_operator, having_value, relation_edge,
                            qualified_fields, round_places, order_table, extra_group_column,
                            group_secondary_target, group_secondary_direction,
                            secondary_order_column, secondary_order_direction,
                            per_group_extremum.group_column if per_group_extremum else None,
                            per_group_extremum.extremum_column if per_group_extremum else None,
                            per_group_extremum.function if per_group_extremum else None,
                            (order_table or entity.table) if global_row_extremum else None,
                            order_column if global_row_extremum else None,
                            ({"ASC": "MIN", "DESC": "MAX"}.get(order_direction)
                             if global_row_extremum else None),
                            having_aggregate_function, having_aggregate_column)
        sql, params = compile_select(query, valid_columns,
                                     {joined_table: tuple(item.name for item in joined_table_columns)}
                                     if relation_edge else None)
        program = {"operation": "SELECT", "base_table": query.table,
                   "joined_tables": [query.table] + ([joined_table] if relation_edge else []),
                   "relationship_scope": relationship_scope,
                   "joins": ([{"join": joined_table,
                                "on": f"{relation_edge.child_table}.{relation_edge.child_column} = {relation_edge.parent_table}.{relation_edge.parent_column}"}]
                             if relation_edge else []),
                   "outputs": ["all columns"] if query.output == "rows" else
                              ([f"ROUND({aggregate_function}({aggregate_column}), {round_places})"
                                if round_places is not None else
                                f"{aggregate_function}({aggregate_column})"] if query.output == "aggregate" else
                              ([f"COUNT(DISTINCT {query.columns[0]})"] if query.output == "count" and query.distinct
                               else ["COUNT(*)"] if query.output == "count" else
                               ([f"{field.table}.{field.column}" for field in qualified_fields]
                                if relation_edge else list(query.columns)))),
                   "filters": [asdict(condition) for condition in conditions],
                   "filter_connector": connector,
                   "groups": [{"group by": name} for name in (group_column, extra_group_column) if name],
                   "having": ([{"operator": having_operator, "value": having_value,
                                "measure": ("COUNT(*)" if having_aggregate_function == "COUNT"
                                            else (f"{aggregate_function}({aggregate_column})"))}]
                              if having_operator else []),
                   "order_by": ([{"key": group_column if group_order_target == "key" else
                                   ("row_count" if query.output == "count" else
                                    f"{aggregate_function.lower()}_{aggregate_column}"),
                                   "direction": group_order_direction}] +
                                ([{"key": group_column, "direction": group_tie_direction}]
                                 if group_tie_direction else [])) if group_order_target else
                               (([{"key": f"{order_table}.{order_column}" if order_table else order_column,
                                    "direction": order_direction}] +
                                 ([{"key": secondary_order_column,
                                    "direction": secondary_order_direction}]
                                  if secondary_order_column else []))
                                if order_column else []),
                   "limit": limit, "distinct": "UNSET" if query.output == "count" else
                               (query.distinct if query.distinct else "UNSET"),
                   "extremum": None}
        if grounded_pattern_bindings:
            program["grounded_pattern_bindings"] = grounded_pattern_bindings
        category_domains = small_category_hints(conn, entity.table, columns)
        program["source_invariants"] = {
            name: values[0] for name, values in category_domains.items() if len(values) == 1
        }
        if per_group_extremum:
            program["per_group_extremum"] = {
                "selection": per_group_extremum.shape,
                "group_column": per_group_extremum.group_column,
                "extremum_column": per_group_extremum.extremum_column,
                "function": per_group_extremum.function,
                "ties": ("one" if per_group_extremum.shape == "per_group_representative"
                         else "preserve"),
            }
        if global_row_extremum:
            program["global_extremum"] = {
                "table": order_table or entity.table,
                "column": order_column,
                "function": {"ASC": "MIN", "DESC": "MAX"}[order_direction],
                "ties": "preserve",
            }
        if group_column:
            # GROUP BY keys are selected by the SQL compiler and are part of
            # the answer, so coverage must see them as outputs as well.
            program["outputs"] = [name for name in (group_column, extra_group_column)
                                  if name] + program["outputs"]
        program["typed_query"] = asdict(query)
        if relation_edge:
            grounded_values = []
            for condition in conditions:
                operands = condition.value if isinstance(condition.value, tuple) else (condition.value,)
                for operand in operands:
                    if not isinstance(operand, str):
                        continue
                    for observed, evidence in mechanically_related_values(
                            request, (operand,), column_name=condition.column):
                        if evidence["kind"] != "exact_request_phrase":
                            grounded_values.append({
                                "table": condition.table or entity.table,
                                "column": condition.column,
                                "stored_value": observed,
                                "request_evidence": evidence})
            if grounded_values:
                program["grounded_value_bindings"] = grounded_values
        client.advance(Stage.COVERAGE)
        coverage = resolve_coverage(request, program, client,
                                    source_columns=valid_columns)
        record("Request coverage", coverage.category, coverage)
        if (coverage.category == "missing_order_limit" and order_column and
                not group_column and (limit == 1 or global_row_extremum)):
            ordering_table = order_table or entity.table
            ordering_column = next(column for column in schema_columns[ordering_table]
                                   if column.name == order_column)
            direction_review = review_order_direction(
                request, ordering_table, ordering_column, order_direction, client)
            record("Ordering direction review", direction_review.direction,
                   direction_review)
            if (direction_review.status == "resolved" and
                    direction_review.direction != order_direction):
                order_direction = direction_review.direction
                query = replace(
                    query, order_direction=order_direction,
                    global_extremum_function=(
                        {"ASC": "MIN", "DESC": "MAX"}[order_direction]
                        if global_row_extremum else None))
                sql, params = compile_select(
                    query, valid_columns,
                    {joined_table: tuple(item.name for item in joined_table_columns)}
                    if relation_edge else None)
                program["order_by"][0]["direction"] = order_direction
                if global_row_extremum:
                    program["global_extremum"]["function"] = (
                        {"ASC": "MIN", "DESC": "MAX"}[order_direction])
                program["typed_query"] = asdict(query)
                coverage = resolve_coverage(request, program, client,
                                            source_columns=valid_columns)
                record("Repaired request coverage", coverage.category, coverage)
        if _should_retry_missing_join_filter(
                coverage.category, relation_edge, conditions):
            # Coverage found a real missing WHERE clause after a direct join,
            # while the initial route/filter vote grounded no predicate at all.
            # Reuse the one authoritative predicate planner once with filtering
            # required. It may choose only inspected joined columns/operators/values;
            # if it still cannot ground a predicate, the request remains rejected.
            repair_plan = plan_conditions(
                conn, request, entity, columns, client, facts, record, profiled,
                needs_filter=True, output_kind=output_kind,
                output_fields=chosen_fields,
                grouping=grouping, aggregate_function=aggregate_function,
                aggregate_column=aggregate_column,
                has_group_threshold=has_group_threshold,
                relation_edge=relation_edge, joined_table=joined_table,
                joined_table_columns=joined_table_columns,
                qualified_output_fields=qualified_fields,
                qualified_order_field=(QualifiedField(
                    order_table or entity.table, order_column)
                    if order_column else None),
                excluded_fact_ids=excluded_fact_ids)
            if repair_plan.conditions:
                conditions = list(repair_plan.conditions)
                connector = repair_plan.connector
                if repair_plan.grounded_pattern_bindings:
                    grounded_pattern_bindings.extend(
                        repair_plan.grounded_pattern_bindings)
                query = replace(
                    query, conditions=tuple(conditions), connector=connector)
                sql, params = compile_select(
                    query, valid_columns,
                    {joined_table: tuple(item.name for item in joined_table_columns)})
                program["filters"] = [asdict(condition) for condition in conditions]
                program["filter_connector"] = connector
                if grounded_pattern_bindings:
                    program["grounded_pattern_bindings"] = grounded_pattern_bindings
                program["typed_query"] = asdict(query)
                coverage = resolve_coverage(
                    request, program, client, source_columns=valid_columns)
                record("Repaired request coverage", coverage.category, coverage)
        if (coverage.category == "missing_filter" and not relation_edge
                and _can_append_conjunctive_filter(connector, len(conditions))):
            repair = resolve_missing_null_filter(
                request, columns,
                tuple(asdict(condition) for condition in conditions), client,
                allow_semantic_column_fallback=True)
            if repair.jev_calls:
                record("Missing filter repair",
                       ({"column": repair.column, "operator": repair.operator}
                        if repair.status == "resolved" else None), repair)
            if repair.status == "resolved" and repair.column and repair.operator:
                conditions.append(Condition(repair.column, repair.operator, None))
                query = replace(query, conditions=tuple(conditions), connector="AND")
                sql, params = compile_select(query, valid_columns)
                program["filters"] = [asdict(condition) for condition in conditions]
                program["filter_connector"] = "AND"
                program["typed_query"] = asdict(query)
                coverage = resolve_coverage(
                    request, program, client, source_columns=valid_columns)
                record("Repaired request coverage", coverage.category, coverage)
        if (coverage.category == "missing_filter" and conditions and not relation_edge
                and limit is None and per_group_extremum is None
                and _can_probe_unique_source_row(
                    query.output, query.group_column, query.aggregate_function,
                    query.partition_column)):
            evidence_query = replace(
                query, output="rows", columns=(), distinct=False,
                order_column=None, order_direction=None,
                secondary_order_column=None, secondary_order_direction=None)
            evidence_sql, evidence_params = compile_select(evidence_query, valid_columns)
            evidence_cursor = conn.execute(evidence_sql + " LIMIT 2", evidence_params)
            evidence_rows = evidence_cursor.fetchall()
            if len(evidence_rows) == 1:
                full_row = dict(zip(
                    (item[0] for item in evidence_cursor.description), evidence_rows[0]))
                coverage = review_unique_result_filter(
                    request, program, full_row, client)
                record("Request coverage review", coverage.category, coverage)
        if coverage.status != "resolved":
            raise ValueError(
                f"The compiled plan does not cover the full request ({coverage.category}); "
                "no partial query was executed.")
        if on_event:
            on_event({"kind": "compiled", "program": program, "sql": sql, "params": params})
            on_event({"kind": "phase", "phase": "execute"})
            on_event({"kind": "status", "message": "Executing parameterized SQL in SQLite"})
        cursor = conn.execute(sql, params)
        rows = cursor.fetchmany(MAX_LOADED_ROWS + 1)
        truncated = len(rows) > MAX_LOADED_ROWS
        rows = rows[:MAX_LOADED_ROWS]
        total_rows = len(rows)
        if truncated:
            total_rows = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        output_columns = [description[0] for description in cursor.description]
    return {"supported": True, "program": program, "sql": sql, "params": params,
            "columns": output_columns, "rows": rows, "truncated": truncated,
            "total_rows": total_rows, "vet": None, "steps": steps,
            "stats": {"jev_calls": calls, "input_tokens": input_tokens,
                      "output_tokens": output_tokens, "planner_calls": 0,
                      "vet_calls": 0, "sql_executions": 1 + int(truncated),
                      "schema_queries": 2, "value_queries": len(conditions)}}
