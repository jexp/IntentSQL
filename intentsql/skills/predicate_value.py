"""Choose a condition value from observed data or literal request facts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.facts import LiteralFact, extract_facts
from intentsql.skills.predicate import column_affinity
from intentsql.skills.schema import Column
from intentsql.skills.quantity import decode_scaled_number
from intentsql.skills.role_evidence import role_words
from intentsql.skills.value_hints import mechanically_related_values


_WORDS = re.compile(r"[^\W\d_][\w']*", re.UNICODE)


def has_request_evidence(request: str, value: Any, source: str) -> bool:
    """Reject a sampled value that has no corresponding user evidence."""
    if source in {"request_literal", "request_span", "single_literal",
                  "observed_request_evidence", "jev_grounded_observed_value",
                  "observed_numeric_value", "jev_grounded_observed_numeric"}:
        return True
    if source == "jev_grounded_observed_pattern":
        return True
    if source == "observed_boolean_value":
        return bool(_WORDS.search(request))
    normalized = str(value).casefold().strip()
    if not normalized:
        return False
    if source == "observed_column_value" and len(normalized) <= 2:
        # Short database codes may be the inspected representation of a
        # natural-language request value (for example a one-letter category).
        return bool(_WORDS.search(request))
    return bool(re.search(r"(?<![\w])" + re.escape(normalized) + r"(?![\w])",
                          request.casefold()))


@dataclass(frozen=True)
class PredicateValue:
    value: int | str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    fact_id: str | None = None


def value_candidates(request: str, column: Column, hints: tuple[str, ...],
                     facts: tuple[LiteralFact, ...],
                     operator: str | None = None,
                     excluded_fact_ids: set[str] | None = None,
                     *, date_semantics: bool = False,
                     pattern_hints: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    """Generate bounded choices mechanically; labels carry no inferred meaning."""
    candidates: dict[str, dict[str, Any]] = {}
    excluded_fact_ids = excluded_fact_ids or set()
    if date_semantics:
        for fact in facts:
            if fact.fact_id in excluded_fact_ids:
                continue
            is_year = (fact.kind in ("integer", "worded_number") and
                       isinstance(fact.value, int) and 1000 <= fact.value <= 9999)
            if fact.kind == "date" or is_year:
                candidates[f"literal_{len(candidates)}"] = {
                    "value": fact.value, "source": "request_literal",
                    "offset": fact.start, "fact_id": fact.fact_id}
        return candidates
    if column_affinity(column) == "numeric":
        observed_numeric: list[int | float] = []
        for hint in hints:
            try:
                observed = int(hint) if re.fullmatch(r"[+-]?\d+", hint) else float(hint)
            except ValueError:
                continue
            observed_numeric.append(observed)
        if set(observed_numeric) == {0, 1}:
            # A two-valued numeric domain is a schema-backed boolean encoding.
            # Do not offer unrelated request numbers to this predicate.
            return {f"observed_{index}": {"value": value,
                    "source": "observed_boolean_value"}
                    for index, value in enumerate((0, 1))}
        for fact in facts:
            # SQLite NUMERIC affinity also commonly stores ISO date strings.
            if fact.kind in ("integer", "number", "date", "worded_number") and fact.fact_id not in excluded_fact_ids:
                candidates[f"literal_{len(candidates)}"] = {
                    "value": fact.value, "source": "request_literal", "offset": fact.start,
                    "fact_id": fact.fact_id}
        if not candidates:
            binary = []
            for hint in hints:
                try:
                    value = int(hint) if re.fullmatch(r"[+-]?\d+", hint) else float(hint)
                except ValueError:
                    continue
                if value in (0, 1):
                    binary.append(value)
            if len(set(binary)) == 2:
                # A binary numeric domain is the schema's representation of a
                # boolean condition; Jev maps the request's semantic wording
                # to one of the observed values without inventing a literal.
                for value in sorted(set(binary)):
                    candidates[f"observed_{len(candidates)}"] = {
                        "value": value, "source": "observed_boolean_value"}
            else:
                # No explicit number was written. Bounded inspected values can
                # still encode semantic quantities such as the original/first
                # category or a perfect score. Jev may map the request to one
                # of these exact database operands, or choose NONE; it never
                # regenerates a number.
                for value in list(dict.fromkeys(observed_numeric))[:8]:
                    candidates[f"observed_{len(candidates)}"] = {
                        "value": value, "source": "observed_numeric_value"}
                numeric_values = [item["value"] for item in candidates.values()]
                if numeric_values:
                    low, high = min(numeric_values), max(numeric_values)
                    for item in candidates.values():
                        item["observed_position"] = (
                            "minimum" if item["value"] == low else
                            "maximum" if item["value"] == high else "interior")
        return candidates

    for fact in facts:
        if fact.kind in ("quoted_text", "date"):
            candidates[f"literal_{len(candidates)}"] = {
                "value": fact.value, "source": "request_literal", "offset": fact.start,
                "fact_id": fact.fact_id}
    # Pattern operands are direct user evidence. Sampled database values are
    # intentionally excluded for LIKE-style operators: a matching row is
    # evidence about the schema, never a replacement for the requested text.
    if operator in ("CONTAINS", "PREFIX", "SUFFIX"):
        quoted = [fact for fact in facts if fact.kind == "quoted_text"]
        for fact in quoted:
            candidates[f"request_{len(candidates)}"] = {
                "value": fact.value, "source": "request_literal", "fact_id": fact.fact_id}
        for match in _WORDS.finditer(request):
            # Once the operator has been resolved as a text pattern, even a
            # one-character span is a legitimate bounded operand (for
            # example a name prefix).  Keep it as exact request evidence and
            # let the focused semantic choice assign its role.  The previous
            # length filter silently removed these operands, forcing Jev to
            # choose an unrelated word from the request.
            candidates[f"request_{len(candidates)}"] = {
                "value": match.group(), "source": "request_span",
                "offset": match.start()}
        for pattern in pattern_hints:
            candidates[f"pattern_{len(candidates)}"] = {
                "value": pattern, "source": "observed_pattern_value",
                "evidence": {"kind": "recurring_stored_annotation"},
            }
        return candidates

    request_word_list = [word.casefold() for word in _WORDS.findall(request) if len(word) >= 2]
    request_words = set(request_word_list)
    hint_token_sets = {str(hint): {word.casefold() for word in _WORDS.findall(str(hint))
                                  if len(word) >= 2}
                       for hint in hints}
    token_frequency: dict[str, int] = {}
    for words in hint_token_sets.values():
        for word in words:
            token_frequency[word] = token_frequency.get(word, 0) + 1
    column_words = {word.casefold() for word in _WORDS.findall(column.name.replace("_", " "))
                    if len(word) >= 2}
    matched_hints: list[str] = []
    semantic_evidence: dict[str, dict[str, Any]] = {}
    column_role = set(role_words(request, column.name))
    mechanically_related = dict(mechanically_related_values(
        request, tuple(str(hint) for hint in hints), column_name=column.name))
    mechanically_related_normalized = {str(value).strip(): evidence
                                       for value, evidence in mechanically_related.items()}
    for hint in hints:
        hint_text = str(hint).casefold().strip()
        hint_words = hint_token_sets[str(hint)]
        exact_tokens = sorted(request_words.intersection(hint_words))
        exact_phrase = bool(hint_text and re.search(
            r"(?<![\w])" + re.escape(hint_text) + r"(?![\w])", request.casefold()))
        unique_tokens = sorted(word for word in exact_tokens
                               if token_frequency.get(word, 0) == 1 and
                               word not in column_words)
        normalized_code = re.sub(r"[^0-9A-Za-z]+", "", str(hint)).casefold()
        code_prefixes = []
        if 2 <= len(normalized_code) <= 4:
            code_prefixes = sorted({word for word in request_word_list
                                    if word not in column_role and
                                    len(word) > len(normalized_code) and
                                    word.startswith(normalized_code)})
        if exact_tokens:
            matched_hints.append(hint)
        near_spellings = sorted(
            ((SequenceMatcher(None, hint_text, word).ratio(), word)
             for word in request_word_list
             if len(hint_text) >= 5 and len(word) >= 5 and
             len(hint_text.split()) == 1 and word != hint_text and
             abs(len(hint_text) - len(word)) <= 2), reverse=True)
        if exact_phrase:
            semantic_evidence[str(hint)] = {
                "kind": "exact_observed_phrase",
                "request_text": hint_text,
            }
        elif unique_tokens:
            semantic_evidence[str(hint)] = {
                "kind": "unique_discriminative_token",
                "request_words": unique_tokens,
            }
        elif code_prefixes:
            semantic_evidence[str(hint)] = {
                "kind": "compact_observed_code_prefix",
                "request_words": code_prefixes,
                "normalized_observed_code": normalized_code,
            }
        elif str(hint) in mechanically_related:
            semantic_evidence[str(hint)] = mechanically_related[str(hint)]
        elif str(hint).strip() in mechanically_related_normalized:
            semantic_evidence[str(hint)] = mechanically_related_normalized[str(hint).strip()]
        elif near_spellings and near_spellings[0][0] >= .78:
            semantic_evidence[str(hint)] = {
                "kind": "near_spelling",
                "request_text": near_spellings[0][1],
                "similarity": round(near_spellings[0][0], 2),
            }
        elif near_spellings and near_spellings[0][0] >= .6 and len(hint_text) >= 5 and \
                hint_text[:2] == near_spellings[0][1][:2]:
            semantic_evidence[str(hint)] = {
                "kind": "morphological_neighbor",
                "request_text": near_spellings[0][1],
                "similarity": round(near_spellings[0][0], 2),
            }
    # Short observed codes must remain available even when a misleading long
    # sample has lexical overlap. Jev binds meaning; code supplies stored values.
    if matched_hints:
        matched_hints = list(dict.fromkeys(matched_hints + [hint for hint in hints
            if 1 <= len(str(hint).strip()) <= 4 and
            re.fullmatch(r"[A-Za-z][A-Za-z.]*", str(hint).strip())]))
    for hint in matched_hints:
        if not any(entry["value"] == hint for entry in candidates.values()):
            entry = {"value": hint, "source": "observed_column_value"}
            if str(hint) in semantic_evidence:
                entry["evidence"] = semantic_evidence[str(hint)]
            else:
                entry["evidence"] = {"kind": "exact_token_overlap"}
            candidates[f"observed_{len(candidates)}"] = entry
    # Targeted retrieval is only a bounded candidate generator. When there is
    # no exact token match, retain all retrieved observed values so Jev can map
    # natural wording to the database's actual representation (or choose NONE).
    # Attach only mechanical evidence; never hard-code domain aliases.
    if hints and not candidates:
        for hint in hints:
            if not any(entry["value"] == hint for entry in candidates.values()):
                entry = {"value": hint, "source": "observed_column_value"}
                if str(hint) in semantic_evidence:
                    entry["evidence"] = semantic_evidence[str(hint)]
                candidates[f"observed_{len(candidates)}"] = entry
    if not matched_hints and not candidates:
        # Unknown values still have to come from the user's text. Jev selects
        # the semantic value; this lexical step does not assign a role.
        words = list(dict.fromkeys(match.group() for match in _WORDS.finditer(request)
                                   if len(match.group()) >= 2))[:16]
        for word in words:
            candidates[f"word_{len(candidates)}"] = {
                "value": word, "source": "request_word"}
    return candidates


def resolve_predicate_value(request: str, column: Column, operator: str,
                            hints: tuple[str, ...] = (),
                            client: JevClient | None = None,
                            facts: tuple[LiteralFact, ...] | None = None,
                            excluded_fact_ids: set[str] | None = None,
                            *, date_semantics: bool = False,
                            pattern_hints: tuple[str, ...] = ()) -> PredicateValue:
    """Resolve one operand; NULL and range operators have separate semantics."""
    if operator in ("IS NULL", "IS NOT NULL"):
        return PredicateValue(None, "resolved", "null_operator")
    if operator == "BETWEEN":
        return PredicateValue(None, "unsupported", "range_needs_two_values")
    resolved_facts = facts if facts is not None else extract_facts(request)
    candidates = value_candidates(request, column, hints, resolved_facts, operator,
                                  excluded_fact_ids, date_semantics=date_semantics,
                                  pattern_hints=pattern_hints)
    numeric = column_affinity(column) == "numeric"
    if not candidates:
        if not numeric:
            return PredicateValue(None, "ambiguous", "no_candidate_value")
        return PredicateValue(None, "ambiguous", "no_candidate_value")
    direct_sources = {"request_literal", "request_span", "request_word"}
    exact_literals = [item for item in candidates.values()
                      if item.get("source") == "request_literal"]
    if operator in ("=", "!=") and len(exact_literals) == 1:
        # Equality preserves the exact user operand. A whitespace-padded or
        # otherwise normalized stored sample may describe the data, but may
        # never replace an explicit quoted/date/numeric literal.
        chosen = exact_literals[0]
        return PredicateValue(chosen["value"], "resolved", "request_literal",
                              fact_id=chosen.get("fact_id"))
    strongly_grounded_observed = [item for item in candidates.values()
                                  if item.get("source") == "observed_column_value" and
                                  (item.get("evidence") or {}).get("kind") in
                                  {"exact_observed_phrase"}]
    if len(strongly_grounded_observed) == 1:
        chosen = strongly_grounded_observed[0]
        # Exact phrase evidence establishes the user's token, not storage
        # padding. Alias/code retrieval still preserves the observed operand.
        value = chosen["value"].strip() if isinstance(chosen["value"], str) else chosen["value"]
        return PredicateValue(value, "resolved",
                              "observed_request_evidence")
    observed_without_lexical_evidence = (
        not numeric and
        any(item.get("source") == "observed_column_value" for item in candidates.values()) and
        not any(item.get("source") in direct_sources for item in candidates.values())
    )
    if len(candidates) == 1 and not observed_without_lexical_evidence:
        only = next(iter(candidates.values()))
        return PredicateValue(only["value"], "resolved", only["source"],
                              fact_id=only.get("fact_id"))
    if numeric and any(item.get("source") == "observed_numeric_value"
                       for item in candidates.values()):
        candidates["none"] = {"value": None, "source": "none",
                              "meaning": "none of these inspected numeric values is expressed"}
    elif observed_without_lexical_evidence:
        # Fail closed instead of forcing Jev to pick the nearest sampled value.
        candidates["none"] = {"value": None, "source": "none",
                              "meaning": "none of the observed values is semantically equivalent"}
    client = client or JevClient()
    state = {"request": request, "condition_column": column.name,
             "comparison": operator,
             "candidate_evidence": {
                 key: item.get("evidence") for key, item in candidates.items()
                 if item.get("evidence") is not None
             },
             "task": "Bind this column's operand. Preserve exact literals; stored codes or annotations must mean the requested value. Choose NONE if none match."}

    question = {"value": {"type": "choice",
                          "instructions": "Which grounded candidate supplies this comparison value, or none?",
                          "criteria": candidates}}
    call = client.call(state, question)
    answer = call.answers["value"]
    selected = answer.get("choice")
    if selected not in candidates:
        raise RuntimeError("Jev selected a value outside supplied candidates")
    chosen = candidates[selected]
    confidence = float(answer.get("confidence", 0.0) or 0.0)
    trace = ({"purpose": "resolve predicate value", "state": state,
              "choices": candidates, "selected": selected,
              "probabilities": answer.get("probabilities"),
              "confidence": confidence,
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    probabilities = {key: float(value) for key, value in
                     (answer.get("probabilities") or {}).items()}
    observed_numeric_keys = [key for key, item in candidates.items()
                             if item.get("source") == "observed_numeric_value"]
    if observed_numeric_keys:
        best_key = max(observed_numeric_keys, key=lambda key: probabilities.get(key, 0))
        best_probability = probabilities.get(best_key, 0)
        none_probability = probabilities.get("none", 0)
        if (selected in {"none", best_key} and best_probability >= .35 and
                abs(best_probability - none_probability) <= .2):
            focused = {"observed": candidates[best_key], "none": candidates["none"]}
            focused_state = {
                "request": request,
                "condition_column": column.name,
                "comparison": operator,
                "observed_candidate": candidates[best_key],
                "task": ("Resolve only whether the semantic quantity in the request denotes this "
                         "exact inspected numeric extremum. Choose NONE if it does not."),
            }
            follow = client.call(focused_state, {"value": {"type": "choice",
                "instructions": "Does this exact inspected numeric value represent the requested semantic quantity?",
                "criteria": focused}})
            follow_answer = follow.answers["value"]
            revised = follow_answer.get("choice")
            if revised not in focused:
                raise RuntimeError("System One selected an invalid numeric-value refinement")
            trace += ({"purpose": "refine close semantic numeric binding",
                       "state": focused_state, "choices": focused,
                       "selected": revised,
                       "probabilities": follow_answer.get("probabilities"),
                       "confidence": follow_answer.get("confidence"),
                       "input_tokens": follow.input_tokens,
                       "output_tokens": follow.output_tokens,
                       "elapsed_ms": follow.elapsed_ms},)
            if revised == "observed":
                return PredicateValue(candidates[best_key]["value"], "resolved",
                                      "jev_grounded_observed_numeric", 2,
                                      call.input_tokens + follow.input_tokens,
                                      call.output_tokens + follow.output_tokens, trace)
            return PredicateValue(None, "ambiguous", "jev_no_semantic_equivalent", 2,
                                  call.input_tokens + follow.input_tokens,
                                  call.output_tokens + follow.output_tokens, trace)
    # If deterministic retrieval found one mechanically related value and the
    # broad choice is indecisive, give Jev one focused equivalence decision.
    # This works whether the first call narrowly chose NONE or the candidate;
    # no domain alias is installed in code.
    related = [(key, item) for key, item in candidates.items()
               if key != "none" and
               ((item.get("evidence") or {}).get("kind") in
               {"compact_observed_code_prefix", "compact_observed_single_code_prefix",
                "request_initialism", "near_spelling", "morphological_neighbor"})]
    if len(related) == 1:
        related_key, related_item = related[0]
        selected_probability = probabilities.get(selected, 0)
        runner_up = max((score for key, score in probabilities.items()
                         if key != selected), default=0)
        indecisive = confidence < .75 and (
            selected_probability < .6 or selected_probability - runner_up < .3)
        if selected == "none" or (selected == related_key and indecisive):
            review_choices = {
                "observed": related_item,
                "none": {"value": None,
                         "meaning": "the observed value is not semantically equivalent"},
            }
            review_state = {
                "request": request,
                "condition_column": column.name,
                "comparison": operator,
                "observed_candidate": related_item,
                "task": "Decide only whether this inspected stored value represents a condition in the request. A short code may name a longer category; do not assume it does. Otherwise choose NONE.",
            }
            review = client.call(review_state, {"equivalence": {"type": "choice",
                "instructions": "Is this observed value the database representation of the user's requested category?",
                "criteria": review_choices}})
            review_answer = review.answers["equivalence"]
            revised = review_answer.get("choice")
            if revised not in review_choices:
                raise RuntimeError("Jev selected an unknown semantic-value refinement")
            review_confidence = float(review_answer.get("confidence", 0.0) or 0.0)
            review_probabilities = review_answer.get("probabilities") or {}
            observed_probability = float(review_probabilities.get("observed") or 0)
            none_probability = float(review_probabilities.get("none") or 0)
            trace += ({"purpose": "review compact observed-code semantic equivalence",
                       "state": review_state, "choices": review_choices,
                       "selected": revised,
                       "probabilities": review_answer.get("probabilities"),
                       "confidence": review_confidence,
                       "input_tokens": review.input_tokens,
                       "output_tokens": review.output_tokens,
                       "elapsed_ms": review.elapsed_ms},)
            decisive_choice = (observed_probability >= .65 and
                               observed_probability - none_probability >= .3)
            if revised == "observed" and (decisive_choice or review_confidence >= .72):
                return PredicateValue(related_item["value"], "resolved",
                                      "jev_grounded_observed_value", 2,
                                      call.input_tokens + review.input_tokens,
                                      call.output_tokens + review.output_tokens,
                                      trace)
            return PredicateValue(None, "ambiguous", "jev_no_semantic_equivalent", 2,
                                  call.input_tokens + review.input_tokens,
                                  call.output_tokens + review.output_tokens, trace)
    if selected == "none":
        return PredicateValue(None, "ambiguous", "jev_no_semantic_equivalent", 1,
                              call.input_tokens, call.output_tokens, trace)
    if selected == "worded":
        decoded = decode_scaled_number(request, f"comparison on {column.name}", client)
        return PredicateValue(decoded.value, "resolved", "jev_scaled_codec", 1 + decoded.jev_calls,
                              call.input_tokens + decoded.input_tokens,
                              call.output_tokens + decoded.output_tokens, trace + decoded.trace)
    source = chosen.get("source", "jev_choice")
    spelling = (chosen.get("evidence") or {}) if source == "observed_column_value" else {}
    if spelling.get("kind") in {"near_spelling", "morphological_neighbor"} and confidence < .75:
        review_state = {"request": request, "condition_column": column.name,
                        "request_word": spelling["request_text"],
                        "observed_value": chosen["value"],
                        "retrieval_evidence": spelling}
        review = client.call(review_state, {"value": {
            "type": "choice",
            "instructions": "Does this observed value denote the request's condition value?",
            "criteria": {"observed": chosen["value"],
                         "none": "No safe match to this observed value"}}})
        answer = review.answers["value"]
        score = float((answer.get("probabilities") or {}).get("observed") or 0)
        trace += ({"purpose": "verify near-spelling observed value",
                   "state": review_state, "score": score,
                   "input_tokens": review.input_tokens,
                   "output_tokens": review.output_tokens,
                   "elapsed_ms": review.elapsed_ms},)
        if answer.get("choice") != "observed" or score < .8:
            return PredicateValue(None, "ambiguous", "uncertain_spelling_match", 2,
                                  call.input_tokens + review.input_tokens,
                                  call.output_tokens + review.output_tokens, trace)
        return PredicateValue(chosen["value"], "resolved",
                              "jev_grounded_observed_value", 2,
                              call.input_tokens + review.input_tokens,
                              call.output_tokens + review.output_tokens, trace)
    if source == "observed_pattern_value":
        selected_probability = probabilities.get(selected, 0)
        runner_up = max((score for key, score in probabilities.items() if key != selected),
                        default=0)
        if confidence < .7 and not (selected_probability >= .5 and
                                    selected_probability - runner_up >= .3):
            review_state = {
                "request": request,
                "condition_column": column.name,
                "comparison": operator,
                "recurring_stored_annotation": chosen["value"],
                "task": ("Decide only semantic equivalence. The annotation was mechanically "
                         "profiled from stored values; do not infer any other database value."),
            }
            review = client.call(review_state, {"equivalent": {"type": "noul",
                "instructions": "Does this exact stored annotation represent the row condition expressed by the user?"}})
            score = float(review.answers["equivalent"].get("noul") or 0)
            trace += ({"purpose": "review recurring annotation semantic equivalence",
                       "state": review_state, "score": score,
                       "input_tokens": review.input_tokens,
                       "output_tokens": review.output_tokens,
                       "elapsed_ms": review.elapsed_ms},)
            if score < .6:
                return PredicateValue(None, "ambiguous", "low_confidence_semantic_pattern", 2,
                                      call.input_tokens + review.input_tokens,
                                      call.output_tokens + review.output_tokens, trace)
            return PredicateValue(chosen["value"], "resolved",
                                  "jev_grounded_observed_pattern", 2,
                                  call.input_tokens + review.input_tokens,
                                  call.output_tokens + review.output_tokens, trace)
        source = "jev_grounded_observed_pattern"
    if (source == "observed_numeric_value" and confidence < .75 and
            not (probabilities.get(selected, 0) >= .6 and
                 probabilities.get(selected, 0) - probabilities.get("none", 0) >= .2)):
        return PredicateValue(None, "ambiguous", "low_confidence_semantic_numeric_value", 1,
                              call.input_tokens, call.output_tokens, trace)
    if source == "observed_column_value" and not has_request_evidence(request, chosen["value"], source):
        selected_probability = probabilities.get(selected, 0)
        runner_up = max((score for key, score in probabilities.items() if key != selected),
                        default=0)
        decisive = selected_probability >= .6 and selected_probability - runner_up >= .3
        if confidence < .75 and not decisive:
            return PredicateValue(None, "ambiguous", "low_confidence_semantic_value", 1,
                                  call.input_tokens, call.output_tokens, trace)
        source = "jev_grounded_observed_value"
    return PredicateValue(chosen["value"], "resolved", source, 1,
                          call.input_tokens, call.output_tokens, trace,
                          chosen.get("fact_id"))
