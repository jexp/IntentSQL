"""Resolve independent WHERE roles from schema and request evidence in one call."""
from dataclasses import dataclass, field
from typing import Any
from intentsql.jev_client import JevClient
from intentsql.skills.role_evidence import role_words
from intentsql.skills.schema import Column


@dataclass(frozen=True)
class PredicateColumns:
    columns: tuple[str, ...]
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    uncertain_columns: tuple[str, ...] = ()


def _observed_domain(values: tuple[str, ...]) -> frozenset[str]:
    return frozenset(str(value).strip().casefold()
                     for value in values if str(value).strip())


def _same_role_encoding(left: frozenset[str], right: frozenset[str]) -> bool:
    """True when the smaller domain is nested in the larger one.

    A partial overlap, including one incidental shared code, is not one role.
    """
    if not left or not right or len(left) > 6 or len(right) > 6:
        return False
    shared = left & right
    if not shared:
        return False
    smaller = left if len(left) <= len(right) else right
    return shared == smaller


def _competing_role_groups(order: tuple[str, ...], scores: dict[str, float],
                           evidence: dict[str, tuple[str, ...]],
                           request: str) -> tuple[tuple[str, ...], ...]:
    """Group plausible columns whose small domains are one nested encoding.

    Columns that each have their own request wording stay independent: those
    are separate restrictions, not one role with two names.
    """
    plausible = [name for name in order if scores.get(name, 0) > .3]
    domains = {name: _observed_domain(evidence.get(name, ())) for name in plausible}
    parent = {name: name for name in plausible}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def unite(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for index, left in enumerate(plausible):
        for right in plausible[index + 1:]:
            if _same_role_encoding(domains[left], domains[right]):
                unite(left, right)
    grouped: dict[str, list[str]] = {}
    for name in plausible:
        domain = domains[name]
        if domain and len(domain) <= 6:
            grouped.setdefault(find(name), []).append(name)
    groups = []
    for names in grouped.values():
        if not 2 <= len(names) <= 4:
            continue
        words = {name: set(role_words(request, name)) for name in names}
        if all(words.values()) and all(not words[left] & words[right]
                                       for index, left in enumerate(names)
                                       for right in names[index + 1:]):
            continue
        groups.append(tuple(names))
    return tuple(groups)


def resolve_predicate_columns(request: str, table: str, columns: tuple[Column, ...],
                              client: JevClient | None = None,
                              computed_measure: str | None = None,
                              value_evidence: dict[str, tuple[str, ...]] | None = None,
                              literal_facts: tuple[dict[str, Any], ...] = (),
                              established_columns: tuple[str, ...] = (),
                              excluded_columns: tuple[str, ...] = ()) -> PredicateColumns:
    """Evidence is a candidate, never proof that a restriction was requested.

    Column roles are independent: batch them without a sequence of competing
    choices that splits probability across several correct WHERE columns.
    Only caller-established semantic facts can remove a candidate.
    """
    if not columns:
        return PredicateColumns((), 'ambiguous', 'no_columns')
    known = {column.name for column in columns}
    if not set(established_columns + excluded_columns) <= known:
        raise ValueError('Uninspected predicate column')
    client = client or JevClient()
    remaining = [column for column in columns
                 if column.name not in established_columns + excluded_columns]
    selected = list(established_columns)
    uncertain = []
    trace = []
    calls = input_tokens = output_tokens = 0
    all_scores: dict[str, float] = {}
    column_types = {column.name: column.declared_type for column in remaining}
    for start in range(0, len(remaining), 24):
        candidates = remaining[start:start + 24]
        state = {'request': request, 'source_table': table,
                 'established_where_columns': established_columns,
                 'exact_request_operands': literal_facts,
                 'columns': {col.name: {'type': col.declared_type,
                     'observed': (value_evidence or {}).get(col.name, ())}
                     for col in candidates}}
        if computed_measure:
            state['computed_measure_not_where'] = computed_measure
        questions = {f'c{i}': {'type': 'noul',
            'instructions': f'Does the request constrain stored {col.name} values to select source rows?',
            'criteria': {'true': 'A row condition, including a range, category, or missing value.',
                         'false': 'Only output, assignment, sorting, grouping, group threshold, or unrelated.'}}
            for i, col in enumerate(candidates)}
        call = client.call(state, questions)
        scores = {col.name: float(call.answers[f'c{i}']['noul'])
                  for i, col in enumerate(candidates)}
        all_scores.update(scores)
        for name, score in scores.items():
            if score >= .7:
                selected.append(name)
            elif score > .3:
                uncertain.append(name)
        calls += 1
        input_tokens += call.input_tokens
        output_tokens += call.output_tokens
        trace.append({'purpose': 'resolve source-row condition roles', 'state': state,
                      'scores': scores, 'uncertain': tuple(uncertain),
                      'input_tokens': call.input_tokens, 'output_tokens': call.output_tokens,
                      'elapsed_ms': call.elapsed_ms})
    evidence = value_evidence or {}
    source = 'jev_batched_roles'
    for group in _competing_role_groups(tuple(column.name for column in remaining),
                                        all_scores, evidence, request):
        criteria: dict[str, Any] = {}
        lookup: dict[str, str] = {}
        for index, name in enumerate(group):
            key = f'c{index}'
            lookup[key] = name
            criteria[key] = {
                'column': name,
                'type': column_types.get(name, ''),
                'observed': evidence.get(name, ()),
                'request_words_sharing_the_column_name': role_words(request, name),
            }
        criteria['multiple'] = ('Each of these columns is a separate source-row restriction.')
        criteria['none'] = ('None of these columns is the qualification the request expresses.')
        choice_state = {
            'request': request,
            'source_table': table,
            'task': ('These columns share a small set of stored values, so one qualification '
                     'could fit more than one of them. Choose the single column whose role '
                     'the request wording refers to. Choose multiple only when the request '
                     'restricts each of these columns separately. Choose none only when '
                     'none of these columns is that qualification.'),
        }
        review = client.call(choice_state, {'column': {
            'type': 'choice',
            'instructions': 'Which column role does this row qualification refer to?',
            'criteria': criteria}})
        answer = review.answers['column']
        decision = answer.get('choice')
        confidence = float(answer.get('confidence') or 0)
        if decision not in criteria:
            raise RuntimeError('Jev selected an uninspected column role')
        calls += 1
        input_tokens += review.input_tokens
        output_tokens += review.output_tokens
        trace.append({'purpose': 'choose the column role for one qualification',
                      'state': choice_state, 'choices': criteria, 'selected': decision,
                      'confidence': confidence,
                      'probabilities': answer.get('probabilities'),
                      'input_tokens': review.input_tokens,
                      'output_tokens': review.output_tokens,
                      'elapsed_ms': review.elapsed_ms})
        selected_set = set(selected)
        if confidence >= .6 and decision in lookup:
            keep = {lookup[decision]}
        elif confidence >= .6 and decision == 'multiple':
            keep = {name for name in group if name in selected_set}
        elif confidence >= .6 and decision == 'none':
            keep = set()
        else:
            already = [name for name in group if name in selected_set]
            grounded = [name for name in already if role_words(request, name)]
            if len(already) <= 1:
                keep = set(already)
            elif grounded:
                keep = set(grounded)
            else:
                keep = set()
        for name in group:
            if name in keep and name not in selected_set:
                selected.append(name)
            elif name not in keep and name in selected_set:
                selected.remove(name)
        uncertain = [name for name in uncertain if name not in group]
        if confidence < .6 and not keep:
            # No grounded column survived an unresolved role choice.
            # The candidates are not predicates, and they are not a resolved absence.
            uncertain.extend(name for name in group if name not in selected)
        source = 'jev_column_role_choice'
    status = ('uncertain_tail' if selected else 'ambiguous') if uncertain else 'resolved'
    return PredicateColumns(tuple(selected), status, source, calls,
                            input_tokens, output_tokens, tuple(trace),
                            tuple(uncertain))


def refine_predicate_candidates(request: str, table: str, columns: tuple[Column, ...],
                                candidates: tuple[str, ...], value_evidence: dict,
                                client: JevClient) -> PredicateColumns:
    """Bounded repair of an unresolved column role; samples remain candidates."""
    criteria = {column.name: {"column": column.name, "type": column.declared_type,
                              "observed_values": value_evidence.get(column.name, ())}
                for column in columns if column.name in candidates}
    criteria.update(none="No WHERE restriction on these columns",
                    ambiguous="More than one or no safely grounded column role")
    state = {"request": request, "source_table": table,
             "task": "Resolve only the uncertain WHERE column. Entity modifiers may describe stored categories or codes. Do not choose a field merely used for output or sorting."}
    call = client.call(state, {"column": {"type": "choice",
        "instructions": "Which candidate column carries the requested row restriction?",
        "criteria": criteria}})
    answer = call.answers["column"]
    selected = answer.get("choice")
    if selected not in criteria:
        raise RuntimeError("Jev selected an uninspected predicate candidate")
    resolved = selected not in {"none", "ambiguous"} and float(answer.get("confidence") or 0) >= .7
    trace = ({"purpose": "refine uncertain WHERE role", "state": state,
              "choices": criteria, "selected": selected, "confidence": answer.get("confidence"),
              "input_tokens": call.input_tokens, "output_tokens": call.output_tokens,
              "elapsed_ms": call.elapsed_ms},)
    return PredicateColumns((selected,) if resolved else (),
                            "resolved" if resolved or selected == "none" else "ambiguous",
                            "jev_uncertain_role_choice", 1, call.input_tokens,
                            call.output_tokens, trace,
                            () if resolved or selected == "none" else candidates)
