# Feasibility: the IntentSQL/Jev approach on Neo4j Cypher

Verdict: **yes — the approach transfers well, arguably better than to SQL.** Every load-bearing mechanism has a direct Cypher counterpart; the graph schema is richer evidence than SQLite PRAGMAs.

## Mechanism mapping

| IntentSQL mechanism | Neo4j counterpart | Notes |
| --- | --- | --- |
| `sqlite_master` + `PRAGMA table_info` (tables/columns) | `CALL apoc.meta.schema()` — labels, properties, observed types, counts | One call returns node *and* relationship schemas |
| `PRAGMA foreign_key_list` (declared FK edges) | `apoc.meta.schema()` relationship entries: `type`, `from`/`to` label pairs, counts | A relationship entry is a *directed, typed* edge with both endpoints' labels — strictly more informative than a bare FK |
| `explicitly_named_source` stem matching | same, over labels | unchanged |
| Source → fields/relations narrowing | source label → its properties + adjacent rel types/labels | one-hop traversal = the FK-hop analog, naturally |
| `Condition` / `SelectQuery` typed plan | `CypherQuery` typed plan (label, properties, predicates, one rel hop, ordering, limit) | compiler emits parameterized Cypher |
| Value hints (probe stored values) | `MATCH (n:Label) RETURN DISTINCT n.prop LIMIT k` probes | same memoization pattern |
| Coverage review + finite repairs | identical shape; reuse `jev_client` unchanged | provider-agnostic |
| Guarded writes (preview/vet/confirm) | read-only session for reads; writes are a later stage | prototype is read-only |

## What the graph changes

- **Joins are first-class.** IntentSQL's hardest SQL problem (does the request need a join?) becomes a labeled edge the schema already describes. "Companies that acquired startups" is one inspected `(acq:Company)-[:ACQUIRED]->(t:Company)` candidate.
- **Direction is evidence.** Rel entries carry `from`/`to`, so the compiler can validate pattern direction the way it validates FK direction.
- **Heterogeneous types.** Property types are *observed*, not declared; the numeric-profile guard (95% finite numeric before SUM/AVG) transfers directly using observed type info from `apoc.meta.schema`.
- **No PK guarantee.** SQL's stable-PK assumption (first-row targeting, representative rows) needs `SHOW CONSTRAINTS`/uniqueness evidence on the graph side — keep that capability out of the prototype or gate it on a uniqueness constraint.

## Constraints / risks

- `apoc.meta.schema()` is sampling-based on large graphs — cached per run like the SQLite inspection, fine for the Alpha scope.
- Capability set must stay bounded: prototype = single-label MATCH, property predicates (comparisons, text contains/prefix/suffix, NULL checks, alternatives), optional one inspected relationship hop, DISTINCT/COUNT/scalar aggregates, grouping, ORDER BY + LIMIT. No variable-length paths, no OPTIONAL MATCH, no subqueries, no writes — refuse those, as IntentSQL refuses window functions.
- APOC must be installed on the target DB (Aura Pro/self-managed ship it; APOC Core suffices).

## Prototype scope

`intentcypher/` package: `apoc.meta.schema()` schema layer → typed graph `ReadState` → Jev skills (label, fields, predicates, ordering, quantity, coverage) → deterministic Cypher compiler with bound parameters → read-only execution. Connection via `.env` (`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`) or CLI flags/programmatic config; refusal is a valid outcome.
