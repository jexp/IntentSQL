# IntentSQL Benchmark v1

This benchmark is a frozen, capability-bounded evaluation of the current IntentSQL compiler. It is intentionally **not** a claim of arbitrary natural-language-to-SQL support.

## Corpus

`cases.json` contains exactly 80 cases:

| Suite | Cases | Purpose |
|---|---:|---|
| Core | 20 | Straightforward requests inside the proven compiler envelope |
| Paraphrase | 20 | Five meanings expressed four different ways each |
| Adversarial | 20 | Harder wording while remaining inside the same supported SQL envelope |
| Expected rejection | 20 | Well-defined SQL tasks intentionally outside the current envelope; safe rejection is success |

The supported suites use only the bundled `cyberchase.db`, `dese.db`, and `moneyball.db` databases. Their SHA256 hashes are frozen in `cases.json`.

The rejection suite is not an accuracy trap: every rejection case is a well-defined database question with executable reference SQL, but it deliberately requires a capability IntentSQL does not currently claim (for example a window function, nested aggregate, multi-hop join, correlated subquery, or derived expression across joins).

## What counts as a pass?

For supported cases, **both** checks must pass:

1. **Semantic-plan correctness** — the typed IntentSQL program must contain the expected source/join/filter/group/order/limit semantics.
2. **Execution equivalence** — executing IntentSQL's SQL must return the same rows as the frozen reference SQL.

Reference SQL is executed independently by the benchmark runner and is never supplied to IntentSQL.

For expected-rejection cases, IntentSQL must refuse the request before SQL execution. A network/provider/API-key error never counts as a valid rejection. If IntentSQL executes a rejection case, the runner reports it as an **unsafe execution**.

## Data preflight

Always run this first:

```bash
python benchmarks/v1/run_benchmark.py --preflight-only
```

Preflight makes **zero Jev calls**. It verifies:

- exactly 20 cases in each suite;
- unique case IDs and prompts;
- exact database SHA256 hashes;
- every reference SQL query executes read-only;
- frozen reference row counts/samples still match;
- supported cases do not accidentally contain known unsupported SQL constructs;
- paraphrases in one family share exactly one database, reference SQL, and semantic target.

If any of those checks fail, the benchmark refuses to run.

## Running

Start IntentSQL normally with a configured provider, then from the repository root:

```bash
python benchmarks/v1/run_benchmark.py --runs 1
```

Useful focused runs:

```bash
python benchmarks/v1/run_benchmark.py --suite core
python benchmarks/v1/run_benchmark.py --suite paraphrase
python benchmarks/v1/run_benchmark.py --suite adversarial
python benchmarks/v1/run_benchmark.py --suite reject
python benchmarks/v1/run_benchmark.py --case ADV-01
```

For a release measurement:

```bash
python benchmarks/v1/run_benchmark.py --runs 3
```

The runner creates exact temporary copies of the frozen databases in the IntentSQL workspace and deletes them afterwards. It never benchmarks against a workspace database that may have been changed by earlier mutation testing.

## Reports

Each run writes JSON and Markdown outside the repository under
`../IntentSQL-artifacts/v1/results/` by default. Set `INTENTSQL_ARTIFACTS`
to use another directory.

The JSON report preserves the full result and, by default, the SSE semantic events (including the exact Jev questions/exchanges exposed by the current tracing UI). The Markdown report summarizes:

- execution accuracy;
- semantic-plan accuracy;
- combined supported accuracy;
- safe rejection accuracy;
- unsafe execution rate;
- stability across repeated runs;
- paraphrase-family consistency;
- median and p95 latency;
- total wall-clock time; total Jev calls, input/output/combined tokens, and estimated cost;
- mean and median calls, tokens, and cost per query;
- every failing case.

A single `--runs 1` pass reports stability as **N/A**. Stability is measured only when each case has multiple independent runs.

Use `--no-events` only when a smaller JSON artifact is desired.

## Why this benchmark is separate from Spider

This is the project benchmark: it tests the exact relational language IntentSQL currently claims. An external Spider subset should be reported separately. Spider cases should be selected mechanically from a declared capability filter with a fixed seed, never by hand-picking examples that already pass.
