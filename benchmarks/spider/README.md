# Spider 1.0 capability-bounded check

The Alpha release uses [sample 7](manifest-7.json): a frozen **40-case eligible subset of the Spider 1.0 development corpus**. This is an external check of a declared SQL grammar, not an official Spider leaderboard score or an estimate for all Spider queries. Samples 1-6 are previously seen development data and were excluded from this selection.

`prepare.py` applies the unchanged gold-SQL filter. It accepts one source table, one selected field or scalar aggregate, at most one literal comparison, optional `DISTINCT`, one ordering term, and an optional bounded `LIMIT`. It excludes joins, grouping, nested queries, set operations, computed expressions, and multi-condition logic. It examines the gold parsed SQL, never question wording or an IntentSQL result. Of 1,034 development cases, 212 passed this mechanical filter. After excluding all 120 cases in the six earlier manifests, 92 remained. Seed `130363` selected 40, and the [manifest](manifest-7.json) was committed before execution. No failed case was replaced.

## Data and reproduction

Spider is distributed under CC BY-SA 4.0. IntentSQL does not redistribute its archive or databases. Download the official Spider 1.0 archive using Google Drive ID `1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J`. Its expected SHA-256 is `00636695dabed6b5f4b8328a16b13e069a2f16591d5efcce57660669c85b121b`.

Put `spider_data.zip` in `spider/` under the directory reported by `python -c "from benchmarks.artifacts import artifact_root; print(artifact_root())"`. Override that directory with `INTENTSQL_ARTIFACTS` if needed. For example, after creating the external artifact directory:

```bash
python -m pip install gdown
mkdir -p ../intentsql-artifacts/spider
python -m gdown 1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J -O ../intentsql-artifacts/spider/spider_data.zip
export INTENTSQL_ARTIFACTS="$(cd .. && pwd)/intentsql-artifacts"
python benchmarks/spider/run.py --sample 7 --preflight-only
python benchmarks/spider/run.py --sample 7
```

Run the last command with IntentSQL and a compatible provider already running at `http://127.0.0.1:7862`. Preflight verifies the archive, manifest, selected database hashes, and gold SQLite results without Jev calls. The live runner uses the same `/api/run` stream as the UI, checks both SQLite rows and the bounded typed semantics, records usage and latency, and refuses to overwrite a completed report. Full provider exchanges and downloaded data stay outside source control. The public [per-case result](result-7.json) omits provider traces and credentials.

The committed result identifies **Jev / `jev-latest`** as its provider/model. Other connection presets were not evaluated by this result.

## Frozen first-run result

These are the original frozen first-run numbers. The compiler was subsequently changed for Alpha safety checks; this report was not overwritten or reclassified as a new unseen score. See [the targeted post-fix regression check](../ALPHA_SAFETY.md).

| Metric | Result |
| --- | ---: |
| Execution **and** typed-plan pass | 30/40 (75%) |
| Execution correctness | 31/40 (77.5%) |
| Typed-plan correctness | 31/40 (77.5%) |
| Safe rejections | 5 |
| Incorrect result executions | 4 |
| Matching rows with different typed semantics | 1 |
| Jev calls | 224 |
| Input / output / total tokens | 194,008 / 28,578 / 222,586 |
| Estimated cost | $0.008148 |
| Median / p95 latency | 1.69s / 3.54s |

The five safe rejections involved unresolved filters or ordering. Incorrect executions involved literal whitespace/value grounding, a negated text value, interpreting "highest rank" as the largest numeric rank, and counting distinct states instead of rows. One latest-date query returned the same row through a tie-preserving maximum while the gold plan used `LIMIT 1`; the typed-plan check correctly marks that difference. These failure cases remain in the frozen result.
