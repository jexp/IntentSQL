"""Read orchestration: inspect schema -> bounded Jev skills -> typed plan
-> parameterized Cypher -> coverage gate -> read-only execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import extract_facts

from intentcypher.cypher_compiler import (Compiled, Condition, CypherQuery,
                                          RelHop, compile_cypher)
from intentcypher.graph_schema import GraphSchema, load_schema
from intentcypher.connection import Neo4jConfig, connect_readonly, read_query
from intentcypher.skills import (SkillResult, resolve_fields, resolve_label,
                                 resolve_ordering, resolve_predicate,
                                 resolve_relationship, resolve_coverage,
                                 resolve_hop_count, resolve_direction,
                                 resolve_second_traversal, resolve_exclusion,
                                 resolve_third_traversal,
                                 review_output_objection,
                                 review_unsupported_traversal)

MAX_RETURNED_ROWS = 500


@dataclass
class CypherAnswer:
    status: str  # answered or refused
    query: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def _absorb(self, result: SkillResult) -> None:
        self.jev_calls += result.jev_calls
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.trace += result.trace


def _limit_from_facts(facts, conditions) -> int | None:
    """Mechanical result cap: the one number not consumed as a filter operand."""
    used_values = {condition.value for condition in conditions}
    candidates = [fact for fact in facts
                  if fact.kind in ("integer", "worded_number")
                  and isinstance(fact.value, int)
                  and fact.value not in used_values
                  and 0 < fact.value <= MAX_RETURNED_ROWS]
    if len(candidates) == 1:
        return candidates[0].value
    return None


def _plain(request: str) -> str:
    return " ".join(request.split())


def run_read(request: str, config: Neo4jConfig,
             client: JevClient | None = None) -> CypherAnswer:
    """Resolve one bounded read against one Neo4j database."""
    request = _plain(request)
    answer = CypherAnswer("refused")
    facts = extract_facts(request)
    driver = connect_readonly(config)
    try:
        schema = load_schema(driver, config.database)
        answer = _plan_and_run(request, driver, config, schema, facts, client)
    finally:
        driver.close()
    return answer


def _plan_and_run(request: str, driver, config: Neo4jConfig, schema: GraphSchema,
                  facts, client: JevClient | None) -> CypherAnswer:
    client = client or JevClient()
    answer = CypherAnswer("refused")

    label = resolve_label(request, schema, client)
    answer._absorb(label)
    if label.status != "resolved":
        answer.reason = "No single source label could be identified for this request."
        return answer
    source = label.value

    rel_result = resolve_relationship(request, schema, source, client)
    answer._absorb(rel_result)
    rel_entry, direction, other, hop_count = None, None, None, 1
    if rel_result.status == "resolved":
        rel_entry, direction = rel_result.value
        other = rel_entry.other_label(source)
        if rel_entry.start_label == rel_entry.end_label:
            hops = resolve_hop_count(request, rel_entry, client)
            answer._absorb(hops)
            if hops.status == "resolved":
                hop_count = hops.value
            # Self-loop direction is semantic, not derivable from the schema.
            way = resolve_direction(request, rel_entry, source, client)
            answer._absorb(way)
            if way.status == "resolved":
                direction = way.value

    # A parallel second traversal of the same relationship from the source
    # (co-mention pattern): one hop grounds the anchor, the other supplies
    # the 'other' entities.
    second_hop = None
    third_hop = None
    third_label = None
    if rel_entry:
        second = resolve_second_traversal(request, rel_entry, client)
        answer._absorb(second)
        if second.status == "resolved":
            second_hop = RelHop(rel_entry.rel_type, direction, other)
            third = resolve_third_traversal(request, schema, other, client)
            answer._absorb(third)
            if third.status == "resolved":
                third_rel, third_direction = third.value
                third_hop = RelHop(third_rel.rel_type, third_direction,
                                   third_rel.other_label(other))
                third_label = third_hop.other_label

    projection: tuple[str, ...] = ()
    fields = resolve_fields(request, schema.properties_for(source), client)
    answer._absorb(fields)
    if fields.status == "resolved":
        projection = tuple(fields.value)  # empty selection: whole nodes

    rel_projection: tuple[str, ...] = ()
    if other:
        rel_fields = resolve_fields(request, schema.properties_for(other), client,
                                    prefix="_related")
        answer._absorb(rel_fields)
        if rel_fields.status == "resolved":
            rel_projection = tuple(rel_fields.value)

    third_projection: tuple[str, ...] = ()
    if third_label:
        third_fields = resolve_fields(request, schema.properties_for(third_label),
                                      client, prefix="_third")
        answer._absorb(third_fields)
        if third_fields.status == "resolved":
            third_projection = tuple(third_fields.value)

    conditions: tuple[Condition, ...] = ()
    # Bounded predicate loop: one condition per round, up to three, 'none' ends.
    while len(conditions) < 3:
        predicate = resolve_predicate(
            request, schema, source, facts, client, neighbor_label=other,
            second_neighbor_label=(other if second_hop else None))
        answer._absorb(predicate)
        if predicate.status == "resolved":
            conditions = conditions + (predicate.value,)
        elif predicate.status == "ambiguous":
            answer.reason = ("A requested filter could not be grounded in "
                             "inspected schema and request literals; no query "
                             "was executed.")
            return answer
        else:
            break

    # 'Other' semantics: decided after repairs, when the anchor filter exists.
    def apply_exclusion() -> None:
        """Exclude the anchor's value from the parallel hop if requested."""
        if not second_hop or any(condition.target == "p" for condition in conditions):
            return
        anchors = [condition for condition in conditions
                   if condition.value is not None and condition.target == "m"
                   and condition.operator in ("=", "contains")]
        if not anchors:
            return
        exclusion = resolve_exclusion(request, anchors[0], client)
        answer._absorb(exclusion)
        if exclusion.value == "exclude":
            anchor = anchors[0]
            rebuilt = conditions + (Condition(anchor.property, "!=",
                                              anchor.value, target="p"),)
            _rebuild(rebuilt)

    order_by: tuple[tuple[str, str], ...] = ()
    ordering = resolve_ordering(request, schema.properties_for(source), client)
    answer._absorb(ordering)
    if ordering.status == "resolved":
        order_by = (ordering.value,)

    whole_node = not projection

    def build(current_conditions):
        """Typed plan -> compiled Cypher -> reviewable plan snapshot."""
        limit = _limit_from_facts(facts, current_conditions)
        built = CypherQuery(
            label=source,
            projection=projection,
            conditions=current_conditions,
            rel_hop=(RelHop(rel_entry.rel_type, direction, other, hop_count)
                     if rel_entry and direction and other else None),
            second_hop=second_hop,
            third_hop=third_hop,
            rel_projection=rel_projection,
            third_projection=third_projection,
            order_by=order_by,
            limit=limit,
        )
        snapshot = {
            "source_label": source,
            "relationship": ({"type": rel_entry.rel_type, "direction": direction,
                              "other_label": other, "hops": hop_count}
                             if rel_entry else None),
            "second_traversal": bool(second_hop),
            "third_traversal": ({"type": third_hop.rel_type,
                                 "other_label": third_label}
                                if third_hop else None),
            "third_output": list(third_projection),
            "output": "whole nodes" if whole_node else list(projection),
            "related_output": list(rel_projection),
            "filters": [{"property": condition.property,
                         "operator": condition.operator,
                         "value": condition.value,
                         "target": condition.target}
                        for condition in current_conditions],
            "ordering": [{"property": prop, "direction": direction_}
                         for prop, direction_ in order_by],
            "limit": limit,
        }
        return built, compile_cypher(built), snapshot

    def _rebuild(current_conditions) -> None:
        """Rebuild the plan from conditions and re-run one coverage review."""
        nonlocal conditions, query, compiled, plan, coverage
        conditions = current_conditions
        query, compiled, plan = build(conditions)
        answer.plan = plan
        answer.query = compiled.query
        answer.parameters = dict(compiled.parameters)
        coverage = resolve_coverage(request, plan, client)
        answer._absorb(coverage)

    query, compiled, plan = build(conditions)
    answer.plan = plan
    answer.query = compiled.query
    answer.parameters = dict(compiled.parameters)

    coverage = resolve_coverage(request, plan, client)
    answer._absorb(coverage)

    # Finite repair: a missing-filter objection triggers one retry with
    # filtering required, then one fresh coverage review. A vacuous
    # null-check on the repaired property is replaced by the real filter.
    if coverage.value == "missing_filter":
        repaired = resolve_predicate(request, schema, source, facts, client,
                                     neighbor_label=other, require_filter=True,
                                     second_neighbor_label=(other if second_hop
                                                            else None))
        answer._absorb(repaired)
        if repaired.status == "resolved":
            condition = repaired.value
            vacuous = {index for index, existing in enumerate(conditions)
                       if existing.property == condition.property
                       and existing.target == condition.target
                       and existing.operator in ("is_null", "is_not_null")}
            _rebuild(tuple(existing for index, existing in enumerate(conditions)
                           if index not in vacuous) + (condition,))

    # 'Other' semantics: exclude the anchor's value from the parallel hop.
    apply_exclusion()

    # Finite repair: a missing output retries the fields selection with at
    # least one output required, then one fresh review. The missing output
    # may be on the source label or the third-hop label.
    if coverage.value == "missing_output":
        if third_label and not third_projection:
            repaired_third = resolve_fields(request,
                                           schema.properties_for(third_label),
                                           client, prefix="_third",
                                           require_output=True)
            answer._absorb(repaired_third)
            if repaired_third.status == "resolved" and repaired_third.value:
                third_projection = tuple(repaired_third.value)
                query, compiled, plan = build(conditions)
                answer.plan = plan
                answer.query = compiled.query
                answer.parameters = dict(compiled.parameters)
                coverage = resolve_coverage(request, plan, client)
                answer._absorb(coverage)
        if coverage.value == "missing_output":
            # Still missing after the third-label repair: re-ask the source
            # label's outputs (a wrongly chosen field may be the objection).
            repaired_fields = resolve_fields(request, schema.properties_for(source),
                                              client, require_output=True)
            answer._absorb(repaired_fields)
            if repaired_fields.status == "resolved" and repaired_fields.value:
                projection = tuple(repaired_fields.value)
                whole_node = False
                query, compiled, plan = build(conditions)
                answer.plan = plan
                answer.query = compiled.query
                answer.parameters = dict(compiled.parameters)
                coverage = resolve_coverage(request, plan, client)
                answer._absorb(coverage)

    # Missing-output objection: one specific review names the exact attribute.
    # A legal pick is added and rechecked once; 'none_missing' completes.
    if coverage.value == "missing_output":
        output_review = review_output_objection(request, plan, schema, client)
        answer._absorb(output_review)
        if output_review.value == "none_missing":
            coverage = SkillResult("resolved", "complete")
        else:
            where, prop = output_review.value
            present = ((where == "source" and prop in projection)
                       or (where == "related" and prop in rel_projection)
                       or (where == "third" and prop in third_projection))
            if present:
                # The objection names an attribute the plan already returns;
                # the objection is ungrounded.
                coverage = SkillResult("resolved", "complete")
            else:
                if where == "source":
                    projection = projection + (prop,)
                    whole_node = False
                elif where == "related":
                    rel_projection = rel_projection + (prop,)
                else:
                    third_projection = third_projection + (prop,)
                query, compiled, plan = build(conditions)
                answer.plan = plan
                answer.query = compiled.query
                answer.parameters = dict(compiled.parameters)
                coverage = resolve_coverage(request, plan, client)
                answer._absorb(coverage)

    # Specific objection review: an "unsupported traversal" objection may
    # misread an inspected relationship the plan already traverses. One review.
    if coverage.value == "unsupported_traversal" and plan.get("relationship"):
        review = review_unsupported_traversal(request, plan, client)
        answer._absorb(review)
        if review.value == "covered":
            coverage = SkillResult("resolved", "complete")

    if coverage.value != "complete":
        answer.reason = ("The request requires semantics this prototype does not "
                         f"cover ({coverage.value}); no partial query was executed.")
        return answer

    rows = read_query(driver, config, compiled.query, compiled.parameters)
    answer.rows = [dict(row) for row in rows][:MAX_RETURNED_ROWS]
    answer.status = "answered"
    return answer


__all__ = ["CypherAnswer", "run_read"]
