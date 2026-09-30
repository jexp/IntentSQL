"""Resolve one row extremum independently inside each source group."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from intentsql.jev_client import JevClient
from intentsql.skills.result_cardinality import resolve_result_cardinality
from intentsql.skills.schema import Column


_PER_GROUP_SCOPE = re.compile(r"\b(?:each|every)\b|\bper\b(?![-\u2010-\u2015])|\bwithin\b", re.I)
_PER_GROUP_ROW_SELECTOR = re.compile(
    r"\b(?:first|last|earliest|latest|lowest|highest|minimum|maximum|"
    r"oldest|newest|one|single)\b", re.I)


def should_probe_per_group_shape(request: str, output_kind: str, *,
                                 grouping_hint: bool, windowing_hint: bool) -> bool:
    """Decide whether the specialized per-group row-shape skill should run.

    The coarse route is advisory, not authoritative.  A strong surface signal
    (one/first/last/etc. together with each/every/per) is enough to let the
    bounded schema-aware shape resolver inspect the request.  The helper does
    not decide a group column, metric, direction, or SQL operation.
    """
    if output_kind == "count":
        return False
    scope_hint = bool(_PER_GROUP_SCOPE.search(request))
    if not scope_hint:
        return False
    route_hint = output_kind in {"fields", "rows"} and (grouping_hint or windowing_hint)
    lexical_shape_hint = bool(_PER_GROUP_ROW_SELECTOR.search(request))
    return route_hint or lexical_shape_hint


@dataclass(frozen=True)
class PerGroupExtremum:
    group_column: str | None
    extremum_column: str | None
    function: str | None
    status: str
    source: str
    jev_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    shape: str = "per_group_extremum"


def resolve_per_group_extremum(request: str, table: str, columns: tuple[Column, ...],
                               client: JevClient, *,
                               iso_date_columns: tuple[str, ...] = (),
                               aggregate_route_hint: bool = False) -> PerGroupExtremum:
    """Distinguish a supported per-group min/max row from general window ranking."""
    choices = {f"c{index}": {"column": column.name,
                                "declared_type": column.declared_type,
                                "primary_key": column.primary_key,
                                "observed_format": (
                                    "ISO date" if column.name in iso_date_columns else None)}
               for index, column in enumerate(columns)}
    state = {
        "request": request,
        "source_table": table,
        "inspected_columns": choices,
        "supported_shapes": (
            "count qualifying rows per group; aggregate one stored column per group; "
            "or return source rows at one MIN/MAX value per group (ties preserved)"
        ),
    }
    call = client.call(state, {
        "shape": {
            "type": "choice",
            "instructions": "Which result shape does the request require?",
            "criteria": {
                "ordinary_rows": "Return qualifying source rows; no independent groups or extremum selector.",
                "global_extremum": "Return rows at one global minimum or maximum; preserve ties, with no per-group ranking.",
                "per_group_extremum": "One minimum or maximum source row per group; tied extrema may all be returned.",
                "per_group_representative": (
                    "Return exactly one source row per group when the user asks for one/arbitrary "
                    "representative and gives no first/last ranking criterion. A single inspected "
                    "primary key is used only as a deterministic selector, not as user meaning."),
                "grouped_count": "Return each group key with the count of qualifying source rows.",
                "grouped_aggregate": "Return each group key with SUM, AVG, MIN, or MAX of a stored column.",
                "unsupported": "Top N per group, row numbers, offsets, percentiles, or another unsupported grouped operation.",
            },
        },
        "group": {
            "type": "choice",
            "instructions": "Which inspected column defines the independent groups?",
            "criteria": choices,
        },
        "extremum": {
            "type": "choice",
            "instructions": "Which column defines the requested first/last event within each group?",
            "criteria": choices,
        },
        "direction": {
            "type": "choice",
            "instructions": "Does the requested row have the minimum or maximum value inside each group?",
            "criteria": {"MIN": "Minimum/earliest/first/lowest value in each group.",
                         "MAX": "Maximum/latest/last/highest value in each group."},
        },
    })
    answers = call.answers
    shape = answers["shape"].get("choice")
    group_key = answers["group"].get("choice")
    extremum_key = answers["extremum"].get("choice")
    function = answers["direction"].get("choice")
    if group_key not in choices or extremum_key not in choices or function not in {"MIN", "MAX"}:
        raise RuntimeError("System One selected an option outside the inspected per-group choices")
    group = choices[group_key]["column"]
    extremum = choices[extremum_key]["column"]
    primary_keys = tuple(column.name for column in columns if column.primary_key)
    if shape == "per_group_representative":
        if len(primary_keys) == 1:
            # "one per group" leaves the row otherwise unconstrained.  Use the
            # inspected single-column PK only to make that arbitrary choice stable
            # and tie-free; do not reinterpret it as "first" in user semantics.
            extremum = primary_keys[0]
            function = "MIN"
        else:
            shape = "unsupported"
    row_per_group_shapes = {"per_group_extremum", "per_group_representative"}
    recognized = shape in {"ordinary_rows", "global_extremum", *row_per_group_shapes,
                           "grouped_count", "grouped_aggregate"}
    supported = recognized and (shape not in row_per_group_shapes or group != extremum)
    selected = {"shape": shape, "group_column": group,
                "extremum_column": extremum, "function": function}
    trace = ({
        "purpose": "resolve per-group row extremum",
        "state": state,
        "choices": {"shape": {
                        "ordinary_rows": "ordinary source rows",
                        "global_extremum": "global tied row extremum",
                        "per_group_extremum": "source row extremum",
                        "per_group_representative": "one deterministic representative source row per group",
                        "grouped_count": "count per group",
                        "grouped_aggregate": "aggregate per group",
                        "unsupported": "unsupported"},
                    "group": choices, "extremum": choices,
                    "direction": {"MIN": "minimum", "MAX": "maximum"}},
        "selected": selected,
        "probabilities": {name: answer.get("probabilities")
                          for name, answer in answers.items()},
        "confidence": {name: answer.get("confidence") for name, answer in answers.items()},
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "elapsed_ms": call.elapsed_ms,
    },)
    calls, input_tokens, output_tokens = 1, call.input_tokens, call.output_tokens
    if shape == "per_group_extremum" and aggregate_route_hint:
        # A coarse aggregate route cannot veto the specialized shape choice.
        # It does identify one unresolved distinction: is MIN/MAX the requested
        # value per group, or a selector for full source rows? Ask only that.
        mentioned_columns = tuple(column.name for column in columns if re.search(
            r"(?<!\w)" + re.escape(column.name).replace("_", r"[_\s]+") + r"(?!\w)",
            request, re.I))
        grain_state = {
            "request": request,
            "source_table": table,
            "established_group": group,
            "established_extremum": {"column": extremum, "function": function},
            "explicit_column_mentions": mentioned_columns,
            "task": "Decide only the output grain; the group and MIN/MAX column are already fixed.",
        }
        grain_criteria = {
            "grouped_value": "One result per group: group key and computed MIN/MAX value only.",
            "source_rows": "Return source rows at each group's MIN/MAX, including requested row fields; ties can return several rows.",
            "ambiguous": "The request does not establish whether it wants grouped values or source rows.",
        }
        grain_call = client.call(grain_state, {"grain": {
            "type": "choice",
            "instructions": "Does the request ask for grouped values or the underlying source rows?",
            "criteria": grain_criteria,
        }})
        grain_answer = grain_call.answers["grain"]
        grain = grain_answer.get("choice")
        if grain not in grain_criteria:
            raise RuntimeError("System One selected an invalid per-group output grain")
        confidence = float(grain_answer.get("confidence") or 0)
        trace += ({"purpose": "distinguish grouped MIN/MAX value from source-row selection",
                   "state": grain_state, "selected": grain,
                   "confidence": confidence,
                   "probabilities": grain_answer.get("probabilities"),
                   "input_tokens": grain_call.input_tokens,
                   "output_tokens": grain_call.output_tokens,
                   "elapsed_ms": grain_call.elapsed_ms},)
        calls += 1
        input_tokens += grain_call.input_tokens
        output_tokens += grain_call.output_tokens
        if confidence < .7 or grain == "ambiguous":
            shape = "unsupported"
        elif grain == "grouped_value":
            shape = "grouped_aggregate"
    if shape in row_per_group_shapes:
        group_label = re.escape(group.removesuffix("_id")).replace("_", r"[_\s]+")
        grouped_scope = bool(re.search(
            r"(?:\b(?:each|every)\b|\bper\b(?![-\u2010-\u2015]))\s+(?:\w+\s+){0,2}" + group_label + r"\b",
            request, re.I))
        if not grouped_scope and _PER_GROUP_SCOPE.search(request):
            unit = re.search(
                r"(?:\b(?:each|every)\b|\bper\b(?![-\u2010-\u2015])|\bwithin\b)\s+"
                r"([A-Za-z_]+(?:\s+[A-Za-z_]+){0,2})", request, re.I)
            # Physical schema spellings are not always the words a person uses.
            # Before rejecting a selected group merely because its column name is
            # not literally present, ask one focused bounded question: does the
            # user's per-group phrase denote the already-selected inspected column?
            scope_state = {
                "request": request,
                "selected_group_column": group,
                "per_group_phrase": unit.group(0) if unit else None,
                "inspected_columns": choices,
                "task": (
                    "Validate only whether the user's each/every/per phrase semantically "
                    "denotes the selected inspected group column. Do not choose a new column "
                    "and do not infer ordering or aggregation here."
                ),
            }
            scope_call = client.call(scope_state, {"scope": {
                "type": "choice",
                "instructions": "Does the per-group phrase refer to the selected group column?",
                "criteria": {
                    "same_group": "Yes; the phrase is a natural-language name for this stored grouping attribute.",
                    "different_scope": "No; the phrase refers to some other unit/scope.",
                },
            }})
            scope_answer = scope_call.answers["scope"]
            scope_choice = scope_answer.get("choice")
            if scope_choice not in {"same_group", "different_scope"}:
                raise RuntimeError("System One selected an invalid per-group scope review")
            scope_confidence = float(scope_answer.get("confidence") or 0)
            same_group = scope_choice == "same_group" and scope_confidence >= .72
            trace += ({"purpose": "ground natural per-group phrase to selected schema column",
                       "state": scope_state,
                       "selected": scope_choice,
                       "confidence": scope_confidence,
                       "probabilities": scope_answer.get("probabilities"),
                       "input_tokens": scope_call.input_tokens,
                       "output_tokens": scope_call.output_tokens,
                       "elapsed_ms": scope_call.elapsed_ms},)
            calls += 1
            input_tokens += scope_call.input_tokens
            output_tokens += scope_call.output_tokens
            if same_group:
                grouped_scope = True
            else:
                ranking_clause = (re.sub(r"\bfor\s*$", "", request[:unit.start()],
                                         flags=re.I).strip(" ,.") if unit else request)
                if shape == "per_group_extremum":
                    # An extrema phrase can still be a global ranking followed by an
                    # unrelated per-unit phrase. Give the bounded cardinality resolver
                    # one chance to recover that interpretation.
                    cardinality = resolve_result_cardinality(ranking_clause, None, client)
                    shape = ("global_extremum" if cardinality.mode == "top_one"
                             else "unsupported")
                    trace += ({"purpose": "review per-unit versus per-group scope",
                               "state": {"ranking_clause": ranking_clause,
                                         "unit_phrase": unit.group(0) if unit else None,
                                         "ungrounded_group_column": group},
                               "selected": shape,
                               "cardinality": cardinality.trace,
                               "input_tokens": cardinality.input_tokens,
                               "output_tokens": cardinality.output_tokens},)
                    calls += cardinality.jev_calls
                    input_tokens += cardinality.input_tokens
                    output_tokens += cardinality.output_tokens
                else:
                    # "one per X" has no defensible global fallback.
                    shape = "unsupported"
    supported = shape in {"ordinary_rows", "global_extremum", *row_per_group_shapes,
                          "grouped_count", "grouped_aggregate"} and (
                              shape not in row_per_group_shapes or group != extremum)
    return PerGroupExtremum(
        group if shape in row_per_group_shapes and supported else None,
        extremum if shape in row_per_group_shapes and supported else None,
        function if shape in row_per_group_shapes and supported else None,
        "resolved" if supported else "unsupported",
        "jev_bounded_shape", calls, input_tokens, output_tokens, trace,
        shape or "unsupported")
