# Original CSV compiler experiment

This is a preserved, separate CSV prototype. Its incremental query loop is not the current Alpha web planner described in [ARCHITECTURE.md](../ARCHITECTURE.md).

The CSV supplies the semantic vocabulary (column names and actual values), the engine supplies a tiny universal analytical instruction set, and Jev repeatedly chooses how to assemble an executable query. SQL produces the final answer.

## Files

- `jev_csv_query.py` - the compiler/runtime
- `make_demo_data.py` - deterministic demo CSV generator
- `demo_orders.csv` - generated demo data
- `run_demo.sh` - six test questions

## Setup

```bash
export SYSTEM_ONE_API_KEY='your-key'
# Optional overrides:
# export SYSTEM_ONE_URL='https://api.typesafe.ai/v1/systemone'
# export SYSTEM_ONE_MODEL='jev-latest'
# export JEV_INPUT_USD_PER_MTOK='0.042'
# export JEV_OUTPUT_USD_PER_MTOK='0'
```

No third-party Python packages are required.

## Generate demo data

```bash
python make_demo_data.py
```

## Inspect the discovered schema without spending Jev tokens

```bash
python jev_csv_query.py demo_orders.csv --show-schema-only ignored
```

## Run one question

```bash
python jev_csv_query.py demo_orders.csv \
  "Which country generated the most revenue?"
```

## Run the benchmark set

```bash
./run_demo.sh
```

## What is self-generated?

For each CSV the engine introspects the data and automatically generates:

- aggregate choices such as `SUM(amount)`, `AVG(latency_ms)`, `COUNT(*)`, and `COUNT DISTINCT customer`
- grouping choices from actual columns plus date-derived month/year/day buckets
- filter choices from actual categorical values such as `status = failed` and `region = Europe`
- numeric-threshold filters from numbers present in the user's question
- date filters from actual months in the data
- ordering choices from the actual columns returned by execution

Jev selects among these choices. Its selected operation changes the query state, which generates the next valid choice space.

The planner can execute the partial query, inspect the real result, and then choose to group, filter, sort, limit, change the metric, or answer.

The final answer is the SQLite result itself, not generated prose.

## Current experiment boundary

This first version intentionally targets single-table analytical questions over one CSV: aggregates, grouping, filtering, date buckets, ranking, and top-N. It does not yet do joins, nested HAVING queries, percentages-of-subsets, or free-form text generation.
