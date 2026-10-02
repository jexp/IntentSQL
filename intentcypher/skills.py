"""Bounded Jev skills for the Cypher read graph: candidates from inspected
graph schema, validated selections, nothing model-authored reaches Cypher."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import LiteralFact

from intentcypher.cypher_compiler import Condition
from intentcypher.graph_schema import GraphSchema, Property, RelEntry


def _stem(word: str) -> str:
    word = word.casefold()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [_stem(word) for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", text)]


def explicitly_named_label(request: str, labels: tuple[str, ...]) -> str | None:
    """Resolve a uniquely named label mechanically, without a model call."""
    request_words = _words(request)
    counts: dict[str, int] = {}
    for label in labels:
        parts = _words(label)
        counts[label] = sum(request_words[start:start + len(parts)] == parts
                            for start in range(len(request_words) - len(parts) + 1))
    if not counts:
        return None
    maximum = max(counts.values())
    winners = [label for label, count in counts.items() if count == maximum]
    return winners[0] if maximum and len(winners) == 1 else None


@dataclass(frozen=True)
class SkillResult:
    status: str  # resolved, ambiguous, none
    value: Any = None
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _choice(client: JevClient, question: str, state: dict[str, Any],
            criteria: dict[str, str], instructions: str) -> tuple[str, dict[str, Any]]:
    call = client.call(state, {question: {"type": "choice",
                                          "instructions": instructions,
                                          "criteria": criteria}})
    answer = call.answers[question]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError(f"Jev selected an invalid key for {question!r}")
    trace = {"purpose": question, "state": state, "choices": criteria,
             "selected": selected, "probabilities": answer.get("probabilities"),
             "confidence": answer.get("confidence"),
             "input_tokens": call.input_tokens,
             "output_tokens": call.output_tokens,
             "elapsed_ms": call.elapsed_ms}
    return selected, trace


def resolve_label(request: str, schema: GraphSchema,
                  client: JevClient | None = None) -> SkillResult:
    """Choose one source label from actual inspected labels."""
    labels = schema.label_names
    if not labels:
        return SkillResult("ambiguous")
    if len(labels) == 1:
        return SkillResult("resolved", labels[0])
    named = explicitly_named_label(request, labels)
    if named:
        return SkillResult("resolved", named)
    client = client or JevClient()
    criteria = {f"l{index}": label for index, label in enumerate(labels)}
    criteria["unspecified"] = "No single node label is identified by the request"
    selected, trace = _choice(
        client, "label",
        {"request": request, "candidate_labels": labels,
         "candidate_label_properties": {label: schema.property_names(label)
                                        for label in labels}},
        criteria,
        "Which node label contains the requested source records?")
    if selected == "unspecified":
        return SkillResult("ambiguous", None, 1, trace=trace)
    return SkillResult("resolved", criteria[selected], 1,
                       trace["input_tokens"], trace["output_tokens"], (trace,))


def resolve_relationship(request: str, schema: GraphSchema, label: str,
                         client: JevClient | None = None) -> SkillResult:
    """Decide whether the request needs one inspected relationship traversal."""
    neighbors = schema.neighbors(label)
    criteria: dict[str, str] = {}
    for index, rel in enumerate(neighbors):
        criteria[f"r{index}"] = (f"{rel.rel_type} connecting {label} to "
                                f"{rel.other_label(label)} "
                                f"({'outgoing' if rel.start_label == label else 'incoming'})")
    criteria["none"] = "The request needs only the source label's own properties"
    client = client or JevClient()
    selected, trace = _choice(
        client, "relationship",
        {"request": request, "source_label": label,
         "candidate_relationships": criteria},
        criteria,
        "Does the request need one inspected relationship traversal for a "
        "requested output or filter, or is the source label alone sufficient?")
    if selected == "none":
        return SkillResult("none", None, 1, trace["input_tokens"],
                           trace["output_tokens"], (trace,))
    rel = neighbors[int(selected[1:])]
    direction = "out" if rel.start_label == label else "in"
    return SkillResult("resolved", (rel, direction), 1,
                       trace["input_tokens"], trace["output_tokens"], (trace,))


def resolve_fields(request: str, properties: tuple[Property, ...],
                   client: JevClient | None = None,
                   *, prefix: str = "",
                   require_output: bool = False) -> SkillResult:
    """Choose the requested output properties from the inspected label.

    A bounded selection loop: one property per choice, 'done' ends it,
    at most three output properties. An empty selection means whole nodes.
    With require_output, the first round must select at least one property
    (a whole-request review found a missing output)."""
    names = tuple(prop.name for prop in properties)
    if not names:
        return SkillResult("resolved", ())
    client = client or JevClient()
    selected: list[str] = []
    traces: list[dict[str, Any]] = []
    tokens_in = tokens_out = 0
    for _round in range(min(3, len(names)) + 1):
        remaining = [name for name in names if name not in selected]
        # Stable keys: a property keeps its f-index across rounds.
        criteria = {f"f{index}": name for index, name in enumerate(names)
                    if name in remaining}
        if not (require_output and not selected):
            criteria["done"] = "No further output property is requested"
        chosen, trace = _choice(
            client, f"fields{prefix}",
            {"request": request, "selected_output_properties": selected,
             "candidate_properties": remaining},
            criteria,
            (("A whole-request review found a requested output property "
              "missing. Choose the inspected property the request asks to "
              "return for this"
              f"{' related' if prefix else ''} label.")
             if require_output and not selected else
             ("Which inspected property is a requested output attribute for "
              f"this label{'s related nodes' if prefix else ''}? A request "
              "asking 'what/which X' typically wants X's identifying attribute "
              "(e.g. title or name), not a summary or description. "
              f"Already selected: {selected or 'none'}. Select 'done' when the "
              "requested outputs are complete.")))
        traces.append(trace)
        tokens_in += trace["input_tokens"]
        tokens_out += trace["output_tokens"]
        if chosen == "done":
            break
        selected.append(criteria[chosen])
        if len(selected) >= min(3, len(names)):
            break
    return SkillResult("resolved", tuple(selected), len(traces),
                       tokens_in, tokens_out, tuple(traces))


_NUMERIC_TYPES = {"INTEGER", "FLOAT", "NUMBER", "LONG", "DOUBLE"}
_TEXT_TYPES = {"STRING", "TEXT"}
# Capitalized but clearly not entity names: question/opening function words.
_QUESTION_WORDS = frozenset(
    {"who", "what", "which", "where", "when", "how", "why", "whose", "whom",
     "list", "show", "give", "return", "find", "get", "tell", "name",
     "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
     "for", "from", "of", "in", "on", "at", "by", "to", "and", "or",
     "that", "with", "over", "under", "after", "before", "newest", "oldest",
     "highest", "lowest", "largest", "smallest", "first", "last", "top"})


@dataclass(frozen=True)
class TextWord:
    """A capitalized request word offered as a possible text operand."""
    value: str
    kind: str = "word"

    @property
    def fact_id(self) -> str:
        return f"word:{self.value}"


def legal_operators(prop: Property) -> tuple[str, ...]:
    if any(t in _NUMERIC_TYPES for t in prop.types):
        return ("=", "!=", ">", ">=", "<", "<=", "is_null", "is_not_null")
    if any(t in _TEXT_TYPES for t in prop.types):
        return ("=", "!=", "contains", "starts_with", "ends_with",
                "is_null", "is_not_null")
    return ("=", "!=", "is_null", "is_not_null")


def resolve_predicate(request: str, schema: GraphSchema, label: str,
                      facts: tuple[LiteralFact, ...],
                      client: JevClient | None = None,
                      neighbor_label: str | None = None,
                      require_filter: bool = False,
                      second_neighbor_label: str | None = None) -> SkillResult:
    """Column -> operator -> value, each its own dependent bounded choice.

    When a relationship is selected, the neighbor label's inspected properties
    are legal filter targets too (marked `m.` in the compiled Cypher)."""
    client = client or JevClient()
    properties: list[Property] = list(schema.properties_for(label))
    targets: list[str] = ["n"] * len(properties)
    names: list[str] = [prop.name for prop in properties]
    if neighbor_label:
        for prop in schema.properties_for(neighbor_label):
            properties.append(prop)
            targets.append("m")
            names.append(f"{neighbor_label}.{prop.name}")
    if second_neighbor_label:
        for prop in schema.properties_for(second_neighbor_label):
            properties.append(prop)
            targets.append("p")
            names.append(f"{second_neighbor_label}.{prop.name} (second hop)")
    if not names:
        return SkillResult("none", None)
    criteria = {f"c{index}": name for index, name in enumerate(names)}
    if not require_filter:
        criteria["none"] = "No further property filter is requested"
    instructions = (
        "A whole-request review found a requested filter missing from the plan. "
        "Choose the inspected property it applies to (a named entity in the "
        "request typically filters the related label's name property)."
        if require_filter else
        "Which inspected property does the request filter on? Properties of the "
        "related label are prefixed with that label. A named entity in the "
        "request (see named_entities_in_request) is typically a filter on the "
        "corresponding label's name property. "
        "Select 'none' when no property filter is requested.")
    named_entities = [word for word in re.findall(r"\b[A-Z][A-Za-z0-9]*\b", request)
                      if word.casefold() not in _QUESTION_WORDS]
    selected, column_trace = _choice(
        client, "predicate_column",
        {"request": request, "source_label": label,
         "related_label": neighbor_label or "",
         "candidate_properties": names,
         "named_entities_in_request": named_entities},
        criteria, instructions)
    if selected == "none":
        return SkillResult("none", None, 1,
                           column_trace["input_tokens"],
                           column_trace["output_tokens"], (column_trace,))
    index = int(selected[1:])
    prop = properties[index]
    target = targets[index]
    ops = legal_operators(prop)
    op_criteria = {op: op.replace("_", " ") for op in ops}
    if not require_filter:
        op_criteria["none"] = "No comparison is stated"
    selected_op, op_trace = _choice(
        client, "predicate_operator",
        {"request": request, "source_label": label,
         "condition_property": prop.name,
         "legal_operators": list(ops)},
        op_criteria,
        (f"The request requires a filter on {prop.name!r}; which comparison "
         "does the wording state?" if require_filter else
         f"Which comparison does the request state for {prop.name!r}?"))
    if selected_op == "none":
        return SkillResult("none", None, 2,
                           op_trace["input_tokens"],
                           op_trace["output_tokens"], (column_trace, op_trace))
    if selected_op in ("is_null", "is_not_null"):
        return SkillResult("resolved", Condition(prop.name, selected_op, target=target), 2,
                           op_trace["input_tokens"], op_trace["output_tokens"],
                           (column_trace, op_trace))
    # Boolean operands: a closed true/false choice over the request's wording.
    if "BOOLEAN" in prop.types and selected_op in ("=", "!="):
        bool_criteria = {"true": "the property is stated as present/true",
                         "false": "the property is stated as absent/false"}
        bool_criteria["unspecified"] = "No true/false statement is in the request"
        selected_bool, bool_trace = _choice(
            client, "predicate_value",
            {"request": request, "condition_property": prop.name,
             "comparison": selected_op, "property_type": "BOOLEAN"},
            bool_criteria,
            f"Is {prop.name!r} stated as true or false in the request?")
        if selected_bool == "unspecified":
            return SkillResult("ambiguous", None, 3,
                               bool_trace["input_tokens"],
                               bool_trace["output_tokens"],
                               (column_trace, op_trace, bool_trace))
        value = selected_bool == "true"
        if selected_op == "!=":
            value = not value
        return SkillResult("resolved", Condition(prop.name, "=", value, target=target), 3,
                           bool_trace["input_tokens"], bool_trace["output_tokens"],
                           (column_trace, op_trace, bool_trace))
    # Operand: mechanical from request facts of a matching kind, else a choice.
    numeric = selected_op in ("=", "!=", ">", ">=", "<", "<=") and \
        any(t in _NUMERIC_TYPES for t in prop.types)
    candidates = [fact for fact in facts
                  if (fact.kind == "integer" if numeric else fact.kind == "quoted_text")]
    if not candidates and not numeric and selected_op in ("=", "!=", "contains",
                                                          "starts_with", "ends_with"):
        # Unquoted text operands: capitalized request words are possibilities,
        # not automatic interpretations; a unique one binds mechanically.
        capitalized = [word for word in re.findall(r"\b[A-Z][A-Za-z0-9]*\b", request)
                       if word.casefold() not in _QUESTION_WORDS]
        candidates = [TextWord(word) for word in capitalized]
    if len(candidates) == 1:
        return SkillResult("resolved",
                           Condition(prop.name, selected_op, candidates[0].value, target=target), 2,
                           op_trace["input_tokens"], op_trace["output_tokens"],
                           (column_trace, op_trace))
    if not candidates:
        return SkillResult("ambiguous", None, 2,
                           op_trace["input_tokens"], op_trace["output_tokens"],
                           (column_trace, op_trace))
    value_criteria = {f"v{index}": str(fact.value)
                      for index, fact in enumerate(candidates)}
    value_criteria["unspecified"] = "No listed literal is this comparison's operand"
    selected_value, value_trace = _choice(
        client, "predicate_value",
        {"request": request, "condition_property": prop.name,
         "comparison": selected_op, "candidate_literals": value_criteria},
        value_criteria,
        f"Which extracted request literal is the operand of {prop.name} "
        f"{selected_op.replace('_', ' ')}?")
    if selected_value == "unspecified":
        return SkillResult("ambiguous", None, 3,
                           value_trace["input_tokens"],
                           value_trace["output_tokens"],
                           (column_trace, op_trace, value_trace))
    fact = candidates[int(selected_value[1:])]
    return SkillResult("resolved", Condition(prop.name, selected_op, fact.value, target=target), 3,
                       value_trace["input_tokens"], value_trace["output_tokens"],
                       (column_trace, op_trace, value_trace))


def resolve_ordering(request: str, properties: tuple[Property, ...],
                     client: JevClient | None = None) -> SkillResult:
    """Choose an ordering property and direction from inspected properties."""
    names = tuple(prop.name for prop in properties)
    criteria = {f"o{index}": name for index, name in enumerate(names)}
    criteria["none"] = "No result ordering is requested"
    client = client or JevClient()
    selected, trace = _choice(
        client, "ordering",
        {"request": request, "candidate_properties": names},
        criteria,
        "Which inspected property orders the results? Select 'none' if no "
        "ranking is requested.")
    if selected == "none":
        return SkillResult("none", None, 1, trace["input_tokens"],
                           trace["output_tokens"], (trace,))
    prop = names[int(selected[1:])]
    direction_criteria = {"asc": "lowest/earliest first",
                          "desc": "highest/latest first"}
    selected_direction, direction_trace = _choice(
        client, "ordering_direction",
        {"request": request, "ordering_property": prop},
        direction_criteria,
        f"Which direction does the request order by {prop!r}?")
    return SkillResult("resolved", (prop, selected_direction.upper()), 2,
                       direction_trace["input_tokens"],
                       direction_trace["output_tokens"],
                       (trace, direction_trace))


_COVERAGE = {
    "complete": "The plan fully preserves every material requirement in the request",
    "missing_output": "A requested result property is absent from the plan",
    "missing_filter": "A requested node qualification is absent or wrong",
    "missing_order_limit": "A requested ranking or result count is absent or wrong",
    "unsupported_traversal": "The request needs more graph traversal than one or "
                             "two inspected hops, or different relationship types",
    "unsupported_aggregate": "The request needs aggregation or grouping the plan "
                             "does not represent",
    "unsupported_other": "The request needs semantics outside this prototype's "
                         "capability set",
}


def resolve_coverage(request: str, plan: dict[str, Any],
                     client: JevClient | None = None) -> SkillResult:
    """Independent whole-request review; no construction contract is attached."""
    client = client or JevClient()
    selected, trace = _choice(
        client, "coverage",
        {"request": request, "candidate_plan": plan,
         "named_entities_in_request": [word for word in
                                       re.findall(r"\b[A-Z][A-Za-z0-9]*\b", request)
                                       if word.casefold() not in _QUESTION_WORDS],
         "task": "Review the compiled plan against the whole request. The plan "
                 "may be wrong; challenge earlier selections if material "
                 "semantics are missing or misrepresented. A named entity in "
                 "the request that is not represented by any filter is a "
                 "missing_filter objection."},
        _COVERAGE,
        "Does the plan preserve the request's material semantics?")
    return SkillResult("resolved", selected, 1, trace["input_tokens"],
                       trace["output_tokens"], (trace,))


__all__ = ["SkillResult", "resolve_label", "resolve_relationship", "resolve_fields",
           "resolve_predicate", "resolve_ordering", "resolve_coverage", "review_unsupported_traversal",
           "resolve_hop_count", "resolve_direction",
           "resolve_second_traversal", "resolve_exclusion",
           "resolve_third_traversal", "review_output_objection",
           "legal_operators", "explicitly_named_label"]


def review_unsupported_traversal(request: str, plan: dict[str, Any],
                                 client: JevClient | None = None) -> SkillResult:
    """Distinguish an inspected one-hop traversal from a genuinely unsupported one.

    Mirrors IntentSQL's specific objection reviews: a coverage objection may
    misread a relationship the inspected schema already provides."""
    client = client or JevClient()
    criteria = {
        "covered": ("The planned single inspected relationship traversal "
                    "exactly provides the graph navigation the request states"),
        "unsupported": ("The request needs variable-length, bidirectional, or "
                        "path-finding traversal, or more than two hops of the "
                        "same inspected relationship"),
    }
    selected, trace = _choice(
        client, "traversal_review",
        {"request": request, "planned_relationship": plan.get("relationship"),
         "planned_filters": plan.get("filters"),
         "task": "The plan uses the inspected relationship the stated number "
                 "of times with the stated direction. Decide whether that traversal "
                 "already covers the request's graph navigation, given the filters "
                 "shown."},
        criteria,
        "Does the planned one-hop traversal cover the request's traversal need?")
    return SkillResult("resolved", selected, 1, trace["input_tokens"],
                       trace["output_tokens"], (trace,))


def resolve_hop_count(request: str, rel: RelEntry,
                      client: JevClient | None = None) -> SkillResult:
    """Bounded hop count for a self-loop relationship: one or two hops.

    Only asked when the inspected relationship connects a label to itself
    (e.g. Organization-[:HAS_COMPETITOR]->Organization), so repeating the
    same edge is a legal, schema-grounded extension."""
    client = client or JevClient()
    criteria = {
        "one_hop": "The request traverses this relationship exactly once",
        "two_hops": ("The request traverses the same relationship twice in a "
                     "row (e.g. 'competitors of the competitors')"),
    }
    selected, trace = _choice(
        client, "hop_count",
        {"request": request,
         "self_relationship": {"type": rel.rel_type, "label": rel.start_label,
                               "count": rel.count}},
        criteria,
        "How many times does the request traverse this relationship?")
    return SkillResult("resolved", 1 if selected == "one_hop" else 2, 1,
                       trace["input_tokens"], trace["output_tokens"], (trace,))


def resolve_direction(request: str, rel: RelEntry, label: str,
                      client: JevClient | None = None) -> SkillResult:
    """Direction of a self-loop traversal is semantic, not inspectable:
    (n)-[:T]->(m) vs (n)<-[:T]-(m) mean different things."""
    client = client or JevClient()
    criteria = {
        "out": (f"outgoing: the {label} nodes being asked about point to the "
                f"named/anchor node via {rel.rel_type} (anchor is the start)"),
        "in": (f"incoming: the {label} nodes being asked about are pointed to "
               f"from the named/anchor node via {rel.rel_type}; i.e. the "
               f"requested results have {rel.rel_type} edges to the anchor"),
    }
    selected, trace = _choice(
        client, "traversal_direction",
        {"request": request,
         "self_relationship": {"type": rel.rel_type, "label": label}},
        criteria,
        "Which direction does the request traverse this self-relationship? "
        "'in' means the requested nodes have an edge toward the named anchor "
        "(e.g. 'competitors of X' = nodes that point to X).")
    return SkillResult("resolved", selected, 1, trace["input_tokens"],
                       trace["output_tokens"], (trace,))


def resolve_second_traversal(request: str, rel: RelEntry,
                              client: JevClient | None = None) -> SkillResult:
    """Decide whether the source node also traverses the same relationship a
    second, parallel time (co-mention / co-relation pattern): the anchor hop
    grounds one endpoint, the parallel hop supplies the 'other' entities."""
    client = client or JevClient()
    criteria = {
        "second": (f"the request also asks about a second node reached by "
                   f"another {rel.rel_type} edge from the same source "
                   f"(e.g. 'other companies mentioned by the same articles')"),
        "none": "only the single traversal above is requested",
    }
    selected, trace = _choice(
        client, "second_traversal",
        {"request": request,
         "first_traversal": {"type": rel.rel_type, "label": rel.end_label}},
        criteria,
        "Does the request also traverse this relationship a second, "
        "parallel time from the same source nodes?")
    if selected == "none":
        return SkillResult("none", None, 1, trace["input_tokens"],
                           trace["output_tokens"], (trace,))
    return SkillResult("resolved", rel, 1, trace["input_tokens"],
                       trace["output_tokens"], (trace,))


def resolve_exclusion(request: str, anchor: Condition,
                      client: JevClient | None = None) -> SkillResult:
    """'Other X' semantics: does the parallel hop exclude the anchor's value?"""
    client = client or JevClient()
    criteria = {
        "exclude": (f"the parallel hop must not match the anchor's value "
                    f"({anchor.property} {anchor.operator} "
                    f"{json.dumps(anchor.value, default=str) if anchor.value is not None else ''})"),
        "include": ("the parallel hop may match any value, including the "
                    "anchor's"),
    }
    selected, trace = _choice(
        client, "exclusion",
        {"request": request,
         "anchor_filter": {"property": anchor.property,
                           "operator": anchor.operator,
                           "value": anchor.value}},
        criteria,
        "Does the request's 'other' wording exclude the anchor entity from "
        "the parallel hop's results?")
    return SkillResult("resolved", selected, 1, trace["input_tokens"],
                       trace["output_tokens"], (trace,))


def resolve_third_traversal(request: str, schema: GraphSchema,
                            parallel_label: str,
                            client: JevClient | None = None) -> SkillResult:
    """A further inspected traversal from the parallel hop's node (e.g. the
    board members of the co-mentioned companies)."""
    neighbors = list(schema.neighbors(parallel_label))
    criteria: dict[str, str] = {}
    for index, rel in enumerate(neighbors):
        criteria[f"t{index}"] = (f"{rel.rel_type} connecting the {parallel_label} "
                                 f"nodes to {rel.other_label(parallel_label)}")
    if not criteria:
        return SkillResult("none", None)
    criteria["none"] = "No further traversal from those nodes is requested"
    client = client or JevClient()
    selected, trace = _choice(
        client, "third_traversal",
        {"request": request, "parallel_label": parallel_label,
         "candidate_relationships": criteria},
        criteria,
        "Does the request traverse a further inspected relationship from the "
        "parallel hop's nodes (e.g. 'who is on their board')?")
    if selected == "none":
        return SkillResult("none", None, 1, trace["input_tokens"],
                           trace["output_tokens"], (trace,))
    rel = neighbors[int(selected[1:])]
    direction = "out" if rel.start_label == parallel_label else "in"
    return SkillResult("resolved", (rel, direction), 1,
                       trace["input_tokens"], trace["output_tokens"], (trace,))


def review_output_objection(request: str, plan: dict[str, Any],
                            schema: GraphSchema | None = None,
                            client: JevClient | None = None) -> SkillResult:
    """A missing-output objection gets one specific review: which exact
    attribute is missing, or none (mirrors IntentSQL's objection reviews).

    Value: "none_missing", or ("source"|"related"|"third", property)."""
    client = client or JevClient()
    criteria: dict[str, str] = {}
    mapping: dict[str, tuple[str, str]] = {}
    if schema is not None:
        source = plan.get("source_label")
        for prop in (schema.properties_for(source) if source else ()):
            key = f"s_{prop.name}"
            criteria[key] = f"the request also asks to return {source}.{prop.name}"
            mapping[key] = ("source", prop.name)
        related = (plan.get("relationship") or {}).get("other_label")
        for prop in (schema.properties_for(related) if related else ()):
            key = f"r_{prop.name}"
            criteria[key] = f"the request also asks to return {related}.{prop.name}"
            mapping[key] = ("related", prop.name)
        third = (plan.get("third_traversal") or {}).get("other_label")
        for prop in (schema.properties_for(third) if third else ()):
            key = f"t_{prop.name}"
            criteria[key] = f"the request also asks to return {third}.{prop.name}"
            mapping[key] = ("third", prop.name)
    criteria["none_missing"] = "every requested output attribute is already returned"
    selected, trace = _choice(
        client, "output_review",
        {"request": request,
         "plan_outputs": {"source": plan.get("output"),
                          "related": plan.get("related_output"),
                          "third": plan.get("third_output")},
         "task": "The plan already returns the listed attributes. Name the ONE "
                 "specific attribute the request asks for that is absent, or "
                 "select none_missing."},
        criteria,
        "Which specific requested output attribute is missing from the plan?")
    value = "none_missing" if selected == "none_missing" else mapping[selected]
    return SkillResult("resolved", value, 1, trace["input_tokens"],
                       trace["output_tokens"], (trace,))
