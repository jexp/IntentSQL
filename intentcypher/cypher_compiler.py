"""Deterministic typed-plan to parameterized Cypher compiler (read-only)."""

from __future__ import annotations

from dataclasses import dataclass, field

_OPERATORS = {
    "=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<=",
    "contains": "CONTAINS", "starts_with": "STARTS WITH",
    "ends_with": "ENDS WITH", "is_null": "IS NULL", "is_not_null": "IS NOT NULL",
}
_COMPARISONS = ("=", "!=", ">", ">=", "<", "<=")
_AGGREGATES = ("count", "sum", "avg", "min", "max")
_VARIABLE = "n"


@dataclass(frozen=True)
class Condition:
    property: str
    operator: str
    value: object = None  # unused for is_null / is_not_null
    # n = source label; m = first-hop neighbor; k = second chained hop;
    # p = parallel second traversal from the source (co-mention pattern);
    # q = third traversal from the parallel hop's node
    target: str = "n"

    def __post_init__(self) -> None:
        if self.operator not in _OPERATORS:
            raise ValueError(f"Unsupported operator {self.operator!r}")
        if self.target not in ("n", "m", "k", "p", "q"):
            raise ValueError("Condition target must be a plan variable")


@dataclass(frozen=True)
class RelHop:
    """An inspected directed relationship traversal; hop_count repeats the
    same inspected edge once more (bounded: 1 or 2, self-loops only)."""
    rel_type: str
    direction: str  # "out" (n)->(m) or "in" (n)<-(m)
    other_label: str
    hop_count: int = 1

    def __post_init__(self) -> None:
        if self.hop_count not in (1, 2):
            raise ValueError("Only one or two inspected hops are supported")


@dataclass(frozen=True)
class CypherQuery:
    label: str
    projection: tuple[str, ...] = ()  # empty: return the node
    conditions: tuple[Condition, ...] = ()
    rel_hop: RelHop | None = None
    second_hop: RelHop | None = None  # parallel traversal from the source
    third_hop: RelHop | None = None  # traversal from the parallel hop's node
    rel_projection: tuple[str, ...] = ()  # properties from the neighbor node
    third_projection: tuple[str, ...] = ()  # properties from the third-hop node
    distinct: bool = False
    aggregate: tuple[str, str] | None = None  # (function, property or "*")
    group_by: str | None = None
    order_by: tuple[tuple[str, str], ...] = ()  # (alias, ASC/DESC)
    limit: int | None = None
    logical_op: str = "AND"  # flat AND / OR across conditions

    def __post_init__(self) -> None:
        if self.logical_op not in ("AND", "OR"):
            raise ValueError("Only flat AND/OR condition logic is supported")
        if self.aggregate and self.aggregate[0] not in _AGGREGATES:
            raise ValueError(f"Unsupported aggregate {self.aggregate[0]!r}")
        if self.aggregate and self.rel_hop:
            raise ValueError("Joined aggregates are outside this prototype")
        if self.group_by and not self.aggregate:
            raise ValueError("Grouping requires an aggregate")
        if self.order_by and self.order_by[0][1] not in ("ASC", "DESC"):
            raise ValueError("Order direction must be ASC or DESC")


def quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class Compiled:
    """Parameterized Cypher plus bound parameters; no interpolated values."""
    __slots__ = ("query", "parameters")

    def __init__(self, query: str, parameters: dict[str, object]) -> None:
        self.query = query
        self.parameters = parameters


def compile_cypher(query: CypherQuery) -> Compiled:
    node = quote_identifier(query.label)
    match = f"MATCH ({_VARIABLE}:{node})"
    neighbor_var = "m"
    if query.rel_hop:
        other = quote_identifier(query.rel_hop.other_label)
        rel = quote_identifier(query.rel_hop.rel_type)
        arrow = "-" if query.rel_hop.direction == "out" else "<-"
        close = "->" if query.rel_hop.direction == "out" else "-"
        match += f"{arrow}[:{rel}]{close}(m:{other})"
        if query.rel_hop.hop_count == 2:
            match += f"{arrow}[:{rel}]{close}(k:{other})"
            neighbor_var = "k"
    if query.second_hop:
        # A parallel traversal of the same source, e.g. co-mentioned entities.
        other2 = quote_identifier(query.second_hop.other_label)
        rel2 = quote_identifier(query.second_hop.rel_type)
        arrow2 = "-" if query.second_hop.direction == "out" else "<-"
        close2 = "->" if query.second_hop.direction == "out" else "-"
        match += f", ({_VARIABLE}){arrow2}[:{rel2}]{close2}(p:{other2})"
        neighbor_var = "p"  # the parallel hop's node is the natural output
    if query.third_hop:
        if not query.second_hop:
            raise ValueError("A third traversal requires a parallel second hop")
        other3 = quote_identifier(query.third_hop.other_label)
        rel3 = quote_identifier(query.third_hop.rel_type)
        arrow3 = "-" if query.third_hop.direction == "out" else "<-"
        close3 = "->" if query.third_hop.direction == "out" else "-"
        match += f", (p){arrow3}[:{rel3}]{close3}(q:{other3})"

    parameters: dict[str, object] = {}
    where_parts: list[str] = []
    for index, condition in enumerate(query.conditions):
        if condition.target == "m" and not query.rel_hop:
            raise ValueError("A neighbor filter requires an inspected relationship")
        if condition.target == "p" and not query.second_hop:
            raise ValueError("A parallel-hop filter requires a second traversal")
        if condition.target == "q" and not query.third_hop:
            raise ValueError("A third-hop filter requires a third traversal")
        prop = f"{condition.target}.{quote_identifier(condition.property)}"
        if condition.operator in ("is_null", "is_not_null"):
            where_parts.append(f"{prop} {_OPERATORS[condition.operator]}")
            continue
        param = f"p{index}"
        parameters[param] = condition.value
        where_parts.append(f"{prop} {_OPERATORS[condition.operator]} ${param}")
    where = ""
    if where_parts:
        where = "\nWHERE " + f" {query.logical_op} ".join(where_parts)

    return_clauses: list[str] = []
    if query.group_by and query.aggregate:
        group_prop = quote_identifier(query.group_by)
        func, prop = query.aggregate
        alias = f"{func}_{prop if prop != '*' else 'all'}"
        expression = (f"count({_VARIABLE})" if prop == "*"
                      else f"{func}({_VARIABLE}.{quote_identifier(prop)})")
        returns = ", ".join((f"{_VARIABLE}.{group_prop} AS {group_prop}",
                             f"{expression} AS {alias}"))
        return_clauses.append(f"RETURN {returns}")
        _orderable(query, return_clauses, parameters)
    elif query.aggregate:
        func, prop = query.aggregate
        expression = (f"count({_VARIABLE})" if prop == "*"
                      else f"{func}({_VARIABLE}.{quote_identifier(prop)})")
        return_clauses.append(f"RETURN {expression} AS {func}_{prop if prop != '*' else 'all'}")
        _orderable(query, return_clauses, parameters)
    else:
        distinct = "DISTINCT " if query.distinct else ""
        returns = [f"{_VARIABLE}.{quote_identifier(name)} AS {quote_identifier(name)}"
                   for name in query.projection]
        if query.rel_hop:
            returns.extend(f"{neighbor_var}.{quote_identifier(name)} AS `other_{name}`"
                           for name in query.rel_projection)
        if query.third_hop:
            returns.extend(f"q.{quote_identifier(name)} AS `third_{name}`"
                           for name in query.third_projection)
        if not returns or (query.rel_hop and query.rel_projection
                            and not query.projection):
            # Whole source node, optionally alongside related-node fields.
            returns.insert(0, _VARIABLE)
        return_clauses.append(f"RETURN {distinct}{', '.join(returns)}")
        if query.order_by:
            return_clauses.append("ORDER BY " + ", ".join(
                f"{_VARIABLE}.{quote_identifier(alias)} {direction}"
                for alias, direction in query.order_by))
        if query.limit is not None:
            parameters["limit"] = query.limit
            return_clauses.append("LIMIT $limit")

    return Compiled(match + where + "\n" + "\n".join(return_clauses), parameters)


def _orderable(query: CypherQuery, return_clauses: list[str],
               parameters: dict[str, object]) -> None:
    if query.order_by:
        return_clauses.append("ORDER BY " + ", ".join(
            f"{quote_identifier(alias)} {direction}" for alias, direction in query.order_by))
    if query.limit is not None:
        # LIMIT without ORDER BY keeps Neo4j's unspecified scan order; the plan
        # carries that, the compiler does not invent an ordering.
        parameters["limit"] = query.limit
        return_clauses.append("LIMIT $limit")


__all__ = ["Condition", "CypherQuery", "RelHop", "Compiled", "compile_cypher",
           "quote_identifier"]
