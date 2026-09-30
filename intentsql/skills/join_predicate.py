"""Resolve grounded WHERE attributes across a direct joined pair."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import sqlite3
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.join_fields import QualifiedField
from intentsql.skills.schema import Column, Relation, quote_identifier
from intentsql.skills.predicate import column_affinity
from intentsql.skills.value_hints import mechanically_related_values, targeted_values


@dataclass(frozen=True)
class JoinPredicateColumn:
    field: QualifiedField | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class JoinPredicateColumns:
    fields: tuple[QualifiedField, ...]
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def joined_predicate_candidates(
    conn: sqlite3.Connection, request: str,
    table_columns: dict[str, tuple[Column, ...]],
    numeric_operands: tuple[int | float, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Retrieve plausible qualified columns; leave their semantic role to Jev.

    Exact schema words, observed text candidates, and numeric profiles are
    evidence. If none is found, retain all non-key columns so implicit names
    and unfamiliar synonyms remain answerable.
    """
    words = {word.casefold() for word in re.findall(r"[A-Za-z]+", request)}
    singular = lambda word: (word[:-3] + "y" if word.endswith("ies") else
                             word[:-1] if word.endswith("s") and not word.endswith("ss") else word)
    words = {singular(word) for word in words}
    schema_stopwords = {"per", "by", "of", "in", "to", "from", "the",
                        "and", "or", "for", "with"}
    evidence: dict[str, dict[str, Any]] = {}
    fallback: dict[str, dict[str, Any]] = {}
    text_surfaces: dict[str, list[tuple[frozenset[str], float]]] = {}
    command_words = {"show", "list", "find", "display", "highest", "lowest",
                     "are", "were", "what", "which"}
    for table, columns in table_columns.items():
        for column in columns:
            label = f"{table}.{column.name}"
            key_column = column.primary_key or column.name.casefold().endswith("_id")
            if key_column and not ({"id", "identifier"} & words or
                                   re.search(r"(?<!\w)" + re.escape(column.name) +
                                             r"(?!\w)", request, re.I)):
                continue
            name_words = {singular(word.casefold()) for word in
                          re.findall(r"[A-Za-z]+", column.name)}
            name_matches = tuple(sorted((name_words & words) - schema_stopwords))
            item: dict[str, Any] = {"type": column.declared_type}
            if name_matches:
                item["request_schema_words"] = name_matches
            if column_affinity(column) == "numeric":
                row = conn.execute(
                    f"SELECT MIN({quote_identifier(column.name)}), "
                    f"MAX({quote_identifier(column.name)}) FROM {quote_identifier(table)}"
                ).fetchone()
                if row and all(isinstance(value, (int, float)) for value in row):
                    item["observed_range"] = row
                    if numeric_operands:
                        in_range = tuple(value for value in numeric_operands
                                         if row[0] <= value <= row[1])
                        overlaps = (len(numeric_operands) >= 2 and
                                    min(numeric_operands) <= row[1] and
                                    max(numeric_operands) >= row[0])
                        if in_range or overlaps:
                            item["request_numbers_in_range"] = in_range
                            if overlaps:
                                item["request_interval_overlaps_range"] = True
            elif column_affinity(column) != "numeric":
                observed = targeted_values(conn, table, column.name, request)
                related = mechanically_related_values(
                    request, observed, column_name=column.name)
                if related:
                    strength = {"request_initialism": 4.0,
                                "exact_request_phrase": 3.0,
                                "compact_observed_code_prefix": 2.0,
                                "near_spelling": 1.0}
                    kept = []
                    for value, reason in related:
                        if reason.get("request_phrases"):
                            surfaces = reason["request_phrases"]
                        elif "request_text" in reason:
                            surfaces = (reason["request_text"],)
                        else:
                            surfaces = reason.get("request_words", ())
                        surfaces = tuple(surface for surface in surfaces
                                         if surface not in command_words)
                        if not surfaces and not name_matches:
                            continue
                        kept.append({"value": value, "evidence": reason})
                        score = strength.get(reason.get("kind"), 1.0)
                        if reason.get("kind") == "compact_observed_code_prefix":
                            score += len(reason.get("normalized_observed_code", "")) / 10
                        text_surfaces.setdefault(label, []).extend(
                            (frozenset(surface.split()), score) for surface in surfaces)
                    if kept:
                        item["related_stored_values"] = tuple(kept[:6])
            if (name_matches or item.get("request_numbers_in_range") or
                    item.get("request_interval_overlaps_range") or
                    item.get("related_stored_values")):
                evidence[label] = item
            elif not column.primary_key and not column.name.endswith("_id"):
                fallback[label] = item
    for label, surfaces in text_surfaces.items():
        if (label in evidence and
                not evidence[label].get("request_schema_words") and surfaces and
                all(any(surface <= other_surface and score < other_score
                        for other_label, other_surfaces in text_surfaces.items()
                        if other_label != label
                        for other_surface, other_score in other_surfaces)
                    for surface, score in surfaces)):
            evidence.pop(label)
    return evidence or fallback


def resolve_join_predicate_columns(
    request: str,
    table_columns: dict[str, tuple[Column, ...]],
    client: JevClient | None = None,
    *, candidate_evidence: dict[str, dict[str, Any]] | None = None,
    established_outputs: tuple[str, ...] = (),
    established_order: str | None = None,
    relation: Relation | None = None,
    source_table: str | None = None,
) -> JoinPredicateColumns:
    """Batch independent WHERE roles across one inspected direct join."""
    fields = [(QualifiedField(table, column.name), column.declared_type)
              for table, columns in table_columns.items() for column in columns
              if candidate_evidence is None or
              f"{table}.{column.name}" in candidate_evidence]
    if not fields:
        return JoinPredicateColumns((), "ambiguous", "no_columns")
    client = client or JevClient()
    state = {
        "request": request,
        "candidate_columns": {
            f"c{index}": {"table": field.table, "column": field.column,
                           **(candidate_evidence or {}).get(
                               f"{field.table}.{field.column}", {"type": declared})}
            for index, (field, declared) in enumerate(fields)},
        "established_outputs": established_outputs,
        "established_order": established_order,
        "task": "Bind each restriction to its qualified table.column. Naming a related entity does not also constrain the displayed source name. Output/sort field mentions alone are not operands.",
    }
    if source_table:
        state["source_row_table"] = source_table
    if relation:
        state["foreign_key"] = {
            "child_table": relation.child_table,
            "child_column": relation.child_column,
            "parent_table": relation.parent_table,
            "parent_column": relation.parent_column}
    questions = {f"c{index}": {
        "type": "noul",
        "instructions": f"Does the request supply a comparison operand specifically for {field.table}.{field.column}, rather than just displaying/sorting it or naming a related entity?",
    } for index, (field, _) in enumerate(fields)}
    call = client.call(state, questions)
    scores = {key: float(call.answers[key].get("noul") or 0) for key in questions}
    selected = tuple(field for index, (field, _) in enumerate(fields)
                     if scores[f"c{index}"] >= .6)
    uncertain = tuple(field for index, (field, _) in enumerate(fields)
                      if .3 < scores[f"c{index}"] < .6)
    calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
    status = ("resolved" if selected and not uncertain else
              "uncertain_tail" if selected else "ambiguous")
    trace = ({
        "purpose": "resolve joined predicate columns",
        "state": state,
        "scores": {f"{field.table}.{field.column}": scores[f"c{index}"]
                   for index, (field, _) in enumerate(fields)},
        "selected": tuple(f"{field.table}.{field.column}" for field in selected),
        "uncertain": tuple(f"{field.table}.{field.column}" for field in uncertain),
        "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    def has_operand_evidence(field: QualifiedField) -> bool:
        detail = (candidate_evidence or {}).get(f"{field.table}.{field.column}", {})
        return bool(detail.get("request_schema_words") or
                    detail.get("related_stored_values") or
                    detail.get("request_numbers_in_range") or
                    detail.get("request_interval_overlaps_range"))

    # An established output is not a WHERE column unless the request also
    # supplies an operand for it. Displaying schools.name does not filter it.
    if established_outputs:
        displayed = set(established_outputs)
        selected = tuple(field for field in selected
                         if f"{field.table}.{field.column}" not in displayed
                         or has_operand_evidence(field))
        uncertain = tuple(field for field in uncertain
                          if f"{field.table}.{field.column}" not in displayed
                          or has_operand_evidence(field))
    contenders = tuple(dict.fromkeys((*selected, *uncertain)))

    def role_score(field: QualifiedField) -> float:
        for index, (candidate, _) in enumerate(fields):
            if candidate == field:
                return scores[f"c{index}"]
        return 0.0

    ranked_scores = sorted((role_score(field) for field in contenders), reverse=True)
    score_margin = (ranked_scores[0] - ranked_scores[1]
                    if len(ranked_scores) > 1 else 1.0)
    # Independent yes-votes over undifferentiated columns invent extra
    # predicates. Without operand evidence, one contrastive choice must name
    # a single column or none. A clearly leading column keeps the existing
    # uncertain-tail review. Grounded multi-column filters keep the batch.
    if (contenders and not any(has_operand_evidence(field) for field in contenders) and
            (len(selected) >= 2 or
             (len(selected) == 1 and uncertain and score_margin < .2))):
        criteria: dict[str, Any] = {}
        lookup: dict[str, QualifiedField] = {}
        for index, field in enumerate(contenders):
            key = f"c{index}"
            detail = dict((candidate_evidence or {}).get(
                f"{field.table}.{field.column}", {}))
            detail["column"] = f"{field.table}.{field.column}"
            criteria[key] = detail
            lookup[key] = field
        criteria["none"] = ("None of these columns is the qualification the request expresses.")
        choice_state = {
            "request": request,
            "established_outputs": established_outputs,
            "task": ("Independent votes nominated more than one column, and the request "
                     "does not give each column its own literal. Choose the single column "
                     "the requested row qualification refers to. Choose none only when "
                     "none of these columns is that qualification. Do not keep a second, "
                     "similar measure."),
        }
        review = client.call(choice_state, {"column": {
            "type": "choice",
            "instructions": "Which one column does this row qualification refer to?",
            "criteria": criteria,
        }})
        decision = review.answers["column"].get("choice")
        confidence = float(review.answers["column"].get("confidence") or 0)
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "choose one ungrounded joined condition column",
                   "state": choice_state, "choices": criteria, "selected": decision,
                   "confidence": confidence,
                   "probabilities": review.answers["column"].get("probabilities"),
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if decision not in criteria:
            raise RuntimeError("Jev selected an uninspected joined condition column")
        chosen = lookup.get(decision) if decision != "none" and confidence >= .6 else None
        if chosen is None:
            return JoinPredicateColumns((), "ambiguous", "jev_ungrounded_column_choice",
                                        calls, input_tokens, output_tokens, trace)
        return JoinPredicateColumns((chosen,), "resolved", "jev_ungrounded_column_choice",
                                    calls, input_tokens, output_tokens, trace)
    by_value: dict[str, list[QualifiedField]] = {}
    for field, _ in fields:
        label = f"{field.table}.{field.column}"
        for item in (candidate_evidence or {}).get(label, {}).get("related_stored_values", ()):
            by_value.setdefault(str(item["value"]), []).append(field)
    for value, candidates in by_value.items():
        disputed = tuple(dict.fromkeys(field for field in candidates
                                       if field in selected or field in uncertain))
        if len(disputed) < 2 or not any(field in selected for field in disputed):
            continue
        choices = {f"c{index}": f"{field.table}.{field.column}"
                   for index, field in enumerate(disputed)}
        choices.update(multiple="The request independently constrains several of these columns",
                       ambiguous="The constrained column cannot be determined")
        choice_state = {"request": request, "observed_value": value,
                        "candidate_qualified_columns": tuple(
                            choices[f"c{index}"] for index in range(len(disputed))),
                        "established_outputs": established_outputs,
                        "established_order": established_order}
        if source_table:
            choice_state["source_row_table"] = source_table
        if relation:
            choice_state["foreign_key"] = state["foreign_key"]
        value_start = request.casefold().find(value.casefold())
        if value_start >= 0:
            words_before = [(match.start(), match.group().casefold()) for match in
                            re.finditer(r"[A-Za-z]+", request[:value_start])]
            table_mentions = [(start, table) for start, word in words_before
                              for table in table_columns
                              if (word[:-3] + "y" if word.endswith("ies") else
                                  word[:-1] if word.endswith("s") else word) ==
                                 (table[:-3] + "y" if table.endswith("ies") else
                                  table[:-1] if table.endswith("s") else table).casefold()]
            if table_mentions:
                choice_state["nearest_named_entity_before_value"] = max(table_mentions)[1]
        review = client.call(choice_state, {"column": {"type": "choice",
            "instructions": "Which entity's column does this stated value constrain? Choose multiple only if both are independently required.",
            "criteria": choices}})
        decision = review.answers["column"].get("choice")
        if decision not in choices:
            raise RuntimeError("Jev selected an uninspected joined condition column")
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace += ({"purpose": "disambiguate a shared observed value across joined columns",
                   "state": choice_state, "choices": choices, "selected": decision,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if decision == "ambiguous":
            return JoinPredicateColumns((), "ambiguous", "jev_shared_value_ambiguous",
                                        calls, input_tokens, output_tokens, trace)
        if decision != "multiple":
            chosen = disputed[int(decision[1:])]
            selected = tuple(field for field in selected if field not in disputed) + (chosen,)
            uncertain = tuple(field for field in uncertain if field not in disputed)
            selected = tuple(field for field, _ in fields if field in selected)
    status = ("resolved" if selected and not uncertain else
              "uncertain_tail" if selected else "ambiguous")
    if uncertain:
        unresolved = uncertain
        follow_state = {
            "request": request,
            "established_where_columns": tuple(
                f"{field.table}.{field.column}" for field in selected),
            "uncertain_candidates": {
                f"c{index}": {"qualified_column": f"{field.table}.{field.column}",
                               **(candidate_evidence or {}).get(
                                   f"{field.table}.{field.column}", {})}
                for index, field in enumerate(unresolved)},
            "established_outputs": established_outputs,
            "established_order": established_order,
        }
        follow = client.call(follow_state, {
            key: {"type": "noul", "instructions":
                  f"Is {item['qualified_column']} an additional WHERE condition beyond the established columns?"}
            for key, item in follow_state["uncertain_candidates"].items()})
        follow_scores = tuple(float(follow.answers[f"c{index}"]["noul"])
                              for index in range(len(unresolved)))
        selected += tuple(field for field, score in zip(unresolved, follow_scores)
                          if score >= .6)
        uncertain = tuple(field for field, score in zip(unresolved, follow_scores)
                          if .3 < score < .6)
        status = ("resolved" if selected and not uncertain else
                  "uncertain_tail" if selected else "ambiguous")
        calls += 1
        input_tokens += follow.input_tokens
        output_tokens += follow.output_tokens
        trace += ({"purpose": "refine unresolved joined condition roles",
                   "state": follow_state,
                   "scores": {f"{field.table}.{field.column}": score
                              for field, score in zip(unresolved, follow_scores)},
                   "input_tokens": follow.input_tokens,
                   "output_tokens": follow.output_tokens,
                   "elapsed_ms": follow.elapsed_ms},)
        selected = tuple(field for field, _ in fields if field in selected)
    return JoinPredicateColumns(selected, status, "jev_batched_roles", calls,
                                input_tokens, output_tokens, trace)


def resolve_join_predicate_column(request: str,
                                  table_columns: dict[str, tuple[Column, ...]],
                                  client: JevClient | None = None) -> JoinPredicateColumn:
    fields = [(QualifiedField(table, column.name), column.declared_type)
              for table, columns in table_columns.items() for column in columns]
    if not fields:
        return JoinPredicateColumn(None, "ambiguous", "no_columns")
    client = client or JevClient()
    choices = {f"c{index}": {"table": field.table, "column": field.column,
                              "declared_type": declared}
               for index, (field, declared) in enumerate(fields)}
    state = {"request": request,
             "joined_tables": tuple(table_columns),
             "task": "Choose the qualified attribute whose value determines which joined rows qualify. Ignore output-only columns and foreign-key join columns unless the user independently restricts them."}
    call = client.call(state, {"column": {"type": "choice",
        "instructions": "Which qualified column supplies the requested WHERE condition?",
        "criteria": choices}})
    answer = call.answers["column"]
    selected = answer.get("choice")
    if selected not in choices:
        raise RuntimeError("Jev selected a predicate outside inspected joined columns")
    field = fields[int(selected[1:])][0]
    trace = ({"purpose": "resolve joined predicate column", "state": state,
              "choices": choices, "selected": f"{field.table}.{field.column}",
              "probabilities": answer.get("probabilities"),
              "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens,
              "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return JoinPredicateColumn(field, "resolved", "jev_choice", 1,
                               call.input_tokens, call.output_tokens, trace)
