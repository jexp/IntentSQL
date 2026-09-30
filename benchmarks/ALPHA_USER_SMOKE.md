# Final Alpha user smoke

This is targeted development validation, not a new external benchmark. The
original frozen Spider result remains unchanged.

The [case definitions](../tools/fixtures/alpha_user_cases.json) contain four
reported regressions and 16 fresh requests across the three bundled databases.
Independent SQLite SQL supplies the reference rows; typed-plan checks also
verify filters, measures, grouping, ordering, limits and the absence of extra
filters or HAVING clauses. Runs use disposable imported copies.

Final retained outcomes: **18/18 supported requests matched rows and shape**;
**2/2 unsupported requests rejected before execution**. The four reported
queries passed. One supported join needed a targeted grounding fix and retest;
one fixture was corrected to use the application's Boolean distinct flag.
No observed wrong execution remains in this check.

The existing plain-English smoke suite passed **9/10**. Its shorter singular
joined-extremum request safely rejected uncertain tie/cardinality semantics.
The more explicit name-and-amount version passed. This remains an Alpha wording
limitation, not a correct-query pass. The offline suite passed **379/379** and
the internal v2 preflight passed without provider calls. No large live benchmark
was rerun.

The [curated results](alpha-user-smoke-result.json) exclude raw exchanges and
private paths. The 20 retained outcomes used 159 calls, 148,957 input and 21,098
output tokens (170,055 total), approximately $0.006256, 3.52s median and 7.13s p95.
These totals exclude superseded probes and are not repeatability estimates.

Provider/model identity and run date are **not recorded in the curated result**. Do not infer them from the configured defaults or from a separate historical benchmark.

To reproduce with a running local server and configured provider:

```bash
python tools/alpha_user_smoke.py
python tools/plain_english_smoke.py
```

Generated provider traces and reports are stored outside the repository.
