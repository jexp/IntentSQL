# PLAN — Jev-approach analysis + IntentCypher prototype

## Phase A: IntentSQL analysis (deliverable 1)

- **10** [P1] [done] Explore IntentSQL architecture/code (ARCHITECTURE.md, decision_context.py, jev_client.py, skills/, semantic_read.py)
- **20** [P1] [done] Write `docs/jev-approach-report.md`

## Phase B: Cypher feasibility (deliverable 2)

- **30** [P1] [done] Write `docs/cypher-feasibility.md` (mechanism mapping, risks, scope)

## Phase C: Prototype on branch `feat/intentcypher-prototype` (deliverable 3)

- **40** [P1] [done] Create branch; add `neo4j` dep; write PLAN.md
- **50** [P1] [done] `intentcypher/connection.py` — env/CLI/config connection (NEO4J_URI/USERNAME/PASSWORD/DATABASE), read-only driver session
- **60** [P1] [done] `intentcypher/graph_schema.py` — `apoc.meta.schema()` → typed `GraphSchema` (labels/props/types/counts, rel entries with from/to/count)
- **70** [P1] [done] `intentcypher/cypher_compiler.py` — typed `CypherQuery` → parameterized Cypher; identifier quoting via backticks
- **80** [P1] [done] `intentcypher/skills/` — entity (label choice), fields, predicate (column→operator→value), ordering, quantity, coverage; reuse `intentsql.jev_client`
- **90** [P1] [done] `intentcypher/semantic_cypher.py` — orchestration with graph ReadState
- **100** [P1] [done] `intentcypher/__main__.py` — CLI (`python -m intentcypher "question"`), .env loading
- **110** [P2] [done] Unit tests with fake schema + scripted Jev answers (`tests/test_intentcypher.py`)
- **120** [P2] [done] Integration test against live Neo4j, gated on env creds (`tests/test_intentcypher_integration.py`)
- **125** [P2] [done] `intentcypher/web.py` — minimal web UI (question + connection fields; .env fallback)
- **130** [P1] [done] Present diff + commit message for user confirmation (no auto-commit); awaiting user's `.env` to run the gated integration test

## Next steps (suggested)

- ~~User creates `.env` → run gated integration test~~ **done**: 4/4 live tests passed; end-to-end verified with real Jev (contains-filter, boolean filter, HAS_CEO one-hop traversal, plus two honest refusals at capability boundaries)
- Add repair paths (missing-filter retry, order-direction review) and aggregate/grouping/HAVING skills mirroring IntentSQL
- Per-relationship predicate fields (filter on the neighbor node), multiple output fields
