# IntentSQL: SQL generation with Jev as a decision model

How IntentSQL (Alpha) turns a plain-English request into parameterized SQL, with the Jev (TypeSafe System One) model confined to small, bounded judgments while Python owns evidence, state, compilation and execution.

## Core idea

Code and Jev split the work along a strict line:

| Job | Code (deterministic) | Jev (bounded judgment) |
| --- | --- | --- |
| Evidence | Inspect schema, PK/FKs, stored values; extract literals/dates | Judge semantic role of evidence only when needed |
| Planning | Construct *legal* choices (tables, columns, operations) | Select among those choices |
| State | Store established facts; attach parent context to children | Answer the local question in that context |
| Checks | Validate identifiers, arity, shapes, execution bounds | Whole-request coverage review |
| Execution | Compile parameterized SQL; run it | — no executable code is ever accepted from the model |

Jev never generates SQL. Every decision is a typed question — `choice` (pick a key from supplied criteria), `noul` (yes/no probability), `score` (rubric) — whose answers are validated keys or scalars. The deterministic compiler (`SelectQuery` / `Condition` / `MutationProgram` dataclasses, not strings) can only express capabilities the code implements.

## The read pipeline (`intentsql/semantic_read.py`)

1. **Inspect** (`skills/schema.py`): SQLite metadata → tables, columns, declared FK edges. Read-only, real schema only.
2. **Extract facts** (`skills/facts.py`): numbers, quoted operands, worded quantities, dates — with source spans and stable IDs. Facts are *evidence*, not interpretations.
3. **Route + resolve source** (`skills/entity.py`, `skills/intent.py`): a stem-matching shortcut resolves explicitly named tables without a model call; otherwise a `choice` over actual table names. Selected keys are validated — an out-of-schema pick raises.
4. **Skills narrow as state grows**: fields, aggregate shape, grouping, predicates (column → comparison → operand, each its own dependent question), ordering, quantity/limit. Each skill supplies candidates built *from the selected source* and its inspected neighbors; Jev picks.
5. **Compile + check**: build the typed `SelectQuery`, compile candidate SQL, structurally validate.
6. **Coverage** (`skills/coverage.py`): an *independent* whole-request review (the decision-context contract is deliberately omitted so the reviewer can challenge earlier selections). Unresolved coverage categories block execution.
7. **Finite repairs**: a small table of code-defined reconsideration paths (retry predicates on joined filters, direction review, one NULL-filter choice…). Bounded — no general search until acceptance. Unsupported semantics (e.g. top-N-per-group) are **refused**, and refusal is treated as a valid outcome.

## Why it stays safe and grounded

- **Candidates come from inspection, not imagination.** A FK proves a legal join edge but not that a join is needed; value probes (`skills/value_hints.py`) supply stored values as possibilities.
- **State makes decisions dependent** (`decision_context.py`): an immutable `ReadState` accumulates established facts; `DecisionClient` attaches only a stage-specific subset plus a local scope contract to each question. Children cannot contradict established parents (checked and raised). Evidence IDs are consumed/reserved (a limit's number can't be re-used as a predicate operand).
- **Same model, independent role.** Coverage uses the same provider but without the construction contract — the built plan is reviewed, not defended.
- **Typed everything.** Parameterized SQL only; identifiers quoted; no model-authored strings reach the database.

## Observed results

On the bundled DESE database and the recorded smoke suite, the approach handles rows/fields, comparisons/ranges/text patterns, AND/OR, aggregates + HAVING, one FK-hop joins, ordering/limits and per-group MIN/MAX — and correctly *refuses* unsupported shapes. Curated results are in `benchmarks/`; accuracy is reported separately for rows and typed-plan shape.
