# IntentSQL architecture

IntentSQL composes bounded Jev judgments with deterministic database software. The current Alpha is a finite Python orchestration graph: code constructs choices, advances typed state, validates supported structure and controls execution. This document describes that implementation.

## Code and Jev

| Job | Code | Jev |
| --- | --- | --- |
| Evidence | Inspect schema, PK/FKs and stored values; extract literals/dates; retrieve candidate values | Judge the semantic role of supplied evidence where needed |
| Planning | Construct legal table/column/operation choices and choose the next implemented branch | Select sources, fields, comparisons, operands, measures, grouping and ordering from supplied choices |
| State | Store selected facts and attach relevant parent context to dependent questions | Answer the local question within that context |
| Checks | Validate identifiers, operand arity, literal ownership, query shapes and execution bounds | Review whole-request read coverage or mutation intent/values/scope |
| Reconsideration | Apply the specific conditional paths described below | Make the bounded follow-up judgments those paths request |
| Execution | Compile parameterized SQL; run SQLite reads or guarded writes | No executable code is accepted from answers |

Explicit schema matching and supported literal syntax can bypass some semantic calls. Other branches still ask Jev for shape/role judgments. The orchestration combines these shortcuts with bounded choices and checks.

## The read flow

`web.py` routes requests to read or change. Reads use `semantic_read.run_read`:

1. Open SQLite read-only; inspect tables/columns and extract request facts.
2. Resolve a source and query-shape hints. Invoke the relevant specialized skills.
3. Accumulate selected output, relation, grouping, ranking, predicates and quantity in typed state.
4. Build `SelectQuery` and compile candidate SQL, checking its legal structure.
5. Compare the request with the candidate plan through coverage, applicable reviews and bounded repairs.
6. Execute the accepted answer query, or return a refusal/error.

Inspection and evidence-probe SQL can run before acceptance. Candidate compilation also precedes coverage. The final answer query executes only after coverage resolves.

## Evidence and bounded candidates

`skills/schema.py` reads SQLite metadata and direct declared foreign keys. `skills/facts.py` preserves request numbers, quoted operands, supported worded quantities and dates with source spans and stable IDs. Invalid calendar dates reject.

`skills/value_hints.py` retrieves bounded sets of observed values, spelling/code neighbors and text patterns. These are possibilities, not automatic interpretations. A foreign key establishes a legal edge; it does not establish that the request needs a join.

After a column/date role is grounded, range helpers can bind explicit endpoints deterministically. For numeric aggregation on non-numeric-declared columns, `run_read` profiles the selected column. SUM/AVG normally requires at least 95% finite numeric non-NULL values; recognized ISO-date columns bypass that guard. The profile is recorded, and SQLite's coercion still determines the aggregate—this check does not convert the data.

Schema and numeric profiles are reused within a read run. Predicate value/pattern/date helpers are memoized within the condition-planning invocation. There is no cross-run answer cache.

The active planner uses **Choice** and **Noul**. Candidates come from inspected schema, extracted literal IDs, retrieved stored values or enumerated operations. Skills validate returned keys and provide ambiguity/unsupported exits where appropriate. Independent questions can be batched; dependent questions wait for selected parent facts.

`capabilities.py` is specifically a predicate registry: descriptions, semantic types and operand arity define legal comparisons. Aggregate and relational operations are enumerated elsewhere in skills/compiler code. There is no general model-selected function broker.

## State makes decisions dependent

`decision_context.ReadState` holds source, operation/assignments, literal facts, output/projection, aggregate, groups, relation, ranking, predicates, limit and the current predicate column/comparison. `DecisionClient.advance` replaces this immutable state. Its adapter attaches a stage-specific subset, a local scope contract and the skill's request/evidence to dependent calls.

Concrete constraints in the current code:

- **Source → fields/relations:** candidates are built from the selected table and its inspected direct neighbors.
- **Column → comparison → operand:** child context includes the selected column and then comparison. The adapter rejects contradictory local column/comparison fields; established equality is not later widened into containment.
- **Quantity → operand exclusions:** bound result-limit fact IDs are excluded from later predicate/HAVING candidates. Rounding and HAVING bindings can also reserve their facts. The predicate planner tracks consumed IDs while binding conditions.
- **Aggregate/group/ranking → filters:** relevant selected measures, groups, ranking and per-group facts accompany later filter decisions. Router votes remain hints rather than established conditions.
- **Mutation assignments → old-row filters:** UPDATE masks assignment spans while preserving offsets and passes the operation/new-values context to the shared predicate planner.

This is selected parent state, not an ever-growing transcript. Ownership applies to tracked evidence IDs, not every word. Early calls may carry schema summaries; later context is stage-specific.

The UI streams an evolving selected-decision summary and exposes the final typed program. That summary is not a complete internal-state dump.

## Checking a candidate

The deterministic compiler checks inspected identifiers/qualified fields, valid relations, operand arity, supported output shapes, sorting and resolved values. Identifiers are quoted, LIKE operands escaped and values bound separately. `Condition`, `SelectQuery` and `MutationProgram` are data structures, not generated SQL strings.

`skills/coverage.py` makes a further bounded judgment about the **whole request** against the candidate plan: outputs, filters, relationships, grouping, ordering/cardinality and unsupported expression/Boolean/distinct needs. At the coverage stage, `DecisionClient` omits the construction contract so the reviewer can challenge earlier selections. It uses the same configured provider/model.

An unresolved coverage category stops execution. Specific objection reviews can distinguish a represented text predicate from an unsupported expression, a grouping key from a WHERE filter, or an inspected direct join from an allegedly missing relationship. These reviews change a coverage judgment; they do not necessarily change SQL.

Structural validity and semantic coverage serve different purposes. A legal program may still interpret the request incorrectly; matching reference rows can also conceal a typed-plan error. The runtime coverage judgment is probabilistic. Benchmarks separately check rows and plan shape.

## Finite reconsideration paths

The graph contains these code-defined paths:

| Trigger | Reconsideration | Bound / next step |
| --- | --- | --- |
| Source unresolved and direct-relation context exists | Another source Choice with inspected relation context | One additional source attempt; unresolved source rejects |
| Coarse route needs a specialized result shape | Shape/stored-output skills refine row, scalar, grouped or extremum interpretation | Continue the selected branch or reject unsupported/ambiguous shape |
| Scalar aggregate route has no grouping/HAVING and did not flag source filters | Jev selects an inspected neighbor; code re-anchors to that metric table | Continue local aggregate planning; no joined aggregate capability is added |
| Predicate role/operator/value is uncertain | Skill-specific narrowed follow-up choices | Resolve, retract an ungrounded candidate, or reject; later coverage checks omissions |
| Coverage reports missing order/limit on an existing ungrouped sort with LIMIT 1 or global extremum | One direction review | If direction changes, rebuild and recheck coverage |
| Coverage reports missing filter on a direct join with no initial conditions | Retry the shared predicate planner with filtering required | Once; resolved conditions rebuild the plan and rerun coverage |
| Coverage reports missing local filter and appending AND is legal | One NULL/presence Choice from inspected nullable columns | Confidence-gated; append, rebuild and recheck only if resolved |
| Missing-filter objection on an eligible local ungrouped read with filters and no LIMIT | Probe at most two matching source rows; review if exactly one matches | One semantic review of whether unmatched wording adds a separate restriction |
| A coverage objection may misread represented facts | Specific reviews inside `resolve_coverage` | Finite conditional branches; the missing-output review may check remaining coverage once |

Several paths may apply to one request. Unresolved outcomes still reject; there is no general backtracking or loop until acceptance.

Transport retry is separate. `jev_client.py` can resend the same payload for selected HTTP errors; the default is one retry, configurable through `SYSTEM_ONE_RETRIES` or its constructor. Mutation transport has its own implementation in `read_engine.py`. Network/time-out errors are not semantic reconsideration and are not retried by these paths.

## Guarded writes

`mutations.py` resolves INSERT/UPDATE/DELETE, the inspected target table and assignments. Assignment fields use explicit clause/schema matching or batched judgments; bindings select preserved request evidence rather than generated values.

SELECT, UPDATE and DELETE share `plan_conditions`, `Condition` and `compile_conditions`. Writes use their own operation/assignment orchestration, not the complete read coverage/repair graph. First-row targeting requires an inspected stable primary key.

The write path is:

1. Compile `MutationProgram` and inspect affected rows.
2. Execute a before/after preview on an isolated SQLite copy, with foreign keys enabled.
3. Vet intent, assignment values and scope using Noul judgments plus a PASS/REJECT Choice, with SQL/parameters and preview context.
4. Issue a one-use token for explicit confirmation.
5. On commit, check the database fingerprint and expected affected count inside a transaction. Roll back mismatches or constraint failures.

Preview and commit enforce `MAX_AFFECTED = 100`, including cascades/trigger effects. Unqualified bulk changes, unresolved targets and unsupported mutation shapes reject. The latest commit per database can be undone from a snapshot only while its post-commit fingerprint matches. Confirmation/undo state is process-local.

Bundled sources and imported originals are preserved; writes affect workspace copies. Imported-copy deletion requires confirmation and invalidates associated tokens. SQLite connection helpers explicitly close connections.

## One recorded example

[User-smoke case K1](benchmarks/alpha-user-smoke-result.json) asks:

> Give me the first three episodes from season 4, in episode order.

Its recorded plan is:

```text
source       episodes
output       whole rows
condition    season = 4
ordering     episode_in_season ASC
limit        3
```

```sql
SELECT * FROM "episodes" WHERE "season" = ?
ORDER BY "episode_in_season" ASC LIMIT ?
-- Parameters: [4, 3]
```

The report records three reference rows and passes both execution and semantic checks. It retains the final plan, not raw provider exchanges or a coverage answer, so no intermediate call sequence/confidence is reconstructed here.

The implementation paths explain how these pieces compose: quantity binding reserves its fact ID; the shared condition planner excludes reserved IDs; compiler checks build parameterized SQL; coverage gates answer execution. Those are verified code mechanisms, separate from what this curated record exposes.

## Boundaries and terminology

Reads support local fields/rows, DISTINCT/COUNT, scalar aggregates, flat AND/OR, up to two grouping keys, one HAVING threshold, aggregate rounding from 0–6 places, local primary/secondary ordering and limits. One direct declared-FK join supports base rows/qualified fields, predicates on either table and one joined sort. Per-group MIN/MAX preserves ties; the separate representative shape chooses MIN of an inspected single-column PK. General joined aggregates, multi-hop/self/anti-joins, arbitrary nested Boolean logic, general expressions/subqueries and top-N-per-group ranking are outside this Alpha. The compiler does emit its own bounded extremum subqueries. LIMIT without ordering retains SQLite's unspecified scan order.

Terms used in this repository:

- **Decision model / System One model:** the broad bounded-judgment model concept explored here. “Decision model” is descriptive terminology; [TypeSafe uses System One model](https://docs.typesafe.ai/concepts/system-one) for models returning typed decisions and probabilities.
- **Jev:** TypeSafe’s model/service, named in the recorded internal and Spider experiments.
- **Provider:** IntentSQL’s compatible-endpoint connection abstraction, not the model category or planner.
- **Protocol/interface:** HTTP JSON exchange of `state`, `questions` and an optional configured `model`, returning `answers` and usage; it is separate from the model or connection profile.
- **Choice:** supplied criteria keys and a returned selection validated by the skill.
- **Noul:** the returned yes/no scalar used with branch-specific thresholds, distinct from returned confidence.
- **Score:** renderable answer type; the current planner issues no Score questions.
- **Skill:** a Python resolver for a bounded semantic job, with deterministic evidence and/or a provider call.
- **ReadState:** selected parent facts passed in relevant subsets to later skills.
- **Condition / SelectQuery / MutationProgram:** typed predicate, read and write representations consumed by deterministic compilers.
- **Coverage:** a semantic category review of a candidate read plan against its request.

There is no model training, persistent learning or unrestricted planning/search mechanism. Broader usefulness of this composition remains an experimental question; accepted interpretations are not guaranteed correct.

## Modules and connections

All module paths below are relative to `intentsql/`.

| Module | Responsibility |
| --- | --- |
| [semantic_read.py](intentsql/semantic_read.py) | Read orchestration, shared row predicates, compiler, coverage repairs and execution |
| [decision_context.py](intentsql/decision_context.py) | Typed parent state and stage-specific contracts |
| [capabilities.py](intentsql/capabilities.py) | Closed predicate operations and operand validation |
| [skills/schema.py](intentsql/skills/schema.py), [facts.py](intentsql/skills/facts.py), [value_hints.py](intentsql/skills/value_hints.py) | Schema, literal and stored-value evidence |
| [skills/coverage.py](intentsql/skills/coverage.py) | Whole-request coverage and specific objection reviews |
| [mutations.py](intentsql/mutations.py) | Assignments, shared predicates, write compilation, preview/vet/commit |
| [jev_client.py](intentsql/jev_client.py) | HTTP transport, exchanges, retry and usage |
| [web.py](intentsql/web.py) | Workspace, connection presets, streaming, confirmation and undo |
| [database.py](intentsql/database.py) | Explicit SQLite connection lifetimes |
| [read_engine.py](intentsql/read_engine.py) | Legacy engine used for mutation schema/transport helpers and its CLI; not the current web read planner |

Connection presets cover Jev, OpenJEV, local Laya adapters and custom compatible System One endpoints. Jev/OpenJEV require model identifiers; Laya/custom can omit them. Requests use the same questions/answers interface and optional Bearer authorization. Saved credentials remain outside the repository and are not returned by the connection API.

The [internal v2](benchmarks/v2/README.md) and [original Spider subset](benchmarks/spider/README.md) record Jev / `jev-latest`. Provider/model identity is not recorded in curated [safety](benchmarks/ALPHA_SAFETY.md) or [user-smoke](benchmarks/ALPHA_USER_SMOKE.md) results. Interface compatibility does not establish equivalent measured behavior. The curated internal and Spider reports omit run dates and resolved model IDs. [The `jev-latest` alias can move between releases](https://docs.typesafe.ai/models); its name alone does not identify the historical underlying version.

The app checks host/origin, serializes database work with a process lock and serves versioned/no-store frontend assets. It assumes a local single user and one server worker. Generated artifacts use an external directory configured through `INTENTSQL_ARTIFACTS`; committed manifests and curated evidence are distinct historical snapshots, not a single current accuracy claim.
