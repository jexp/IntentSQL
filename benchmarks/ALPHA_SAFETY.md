# Alpha release safety check

This is a targeted **development regression check**, not a new unseen Spider score. The original [40-case result](spider/result-7.json) remains **30/40** unchanged. The internal [80/80 result](v2/result-alpha.json) also remains a measurement of its recorded compiler version. Neither full live suite was repeated for these fixes.

## What changed

- Exact phrase operands no longer inherit leading/trailing whitespace from stored samples. Quoted operands normalize surrounding formatting whitespace, retain case and raw span provenance, and are preserved for both equality and inequality. Penn Treebank-style quotes are recognized.
- Scalar aggregate selection includes an independent stored-value extremum judgment in the same call. Contradictory MIN/MAX meanings reject instead of silently executing the numeric opposite.
- COUNT DISTINCT must establish its counting unit independently of its target column. A record count remains COUNT(*); an uncertain counting unit rejects.
- Global row extrema resolve one-result versus tie-preserving cardinality. An uncertain singleton cannot silently become all tied rows.
- A missing-filter repair cannot rewrite a multi-condition OR into AND. The current flat IR cannot represent `(A OR B) AND C`; that shape rejects.

## Focused outcomes

| Historical case | Final targeted outcome |
| --- | --- |
| `dev:0250`, destination operand padding | Correct exact `APG` parameter; matches gold SQL |
| `dev:0387`, negated hometown | Preserves exact request case; matches the request-grounded SQLite reference; see caveat below |
| `dev:0439`, highest semantic rank | Safely rejects conflicting numeric/semantic extremum |
| `dev:0569`, last release date | Safely rejects uncertain one-result cardinality |
| `dev:0687`, counting states | COUNT(*); matches gold SQL |

Four neighboring count/distinct/min/max checks on a separate small schema passed. The built-in grouped-range SUM and a direct-join minimum with explicit tie preservation also passed. Together the **11 retained focused checks** had **9 request-reference matches, 2 safe rejections and no reproduced wrong execution**.

The separate release smoke run passed **12/13** cases: ten reads, UPDATE preview/confirmation/commit/undo and DELETE preview. INSERT safely rejected an uncertain role for an omitted nullable date column before SQL compilation. INSERT commit was not tested anew in this pass; the prior **76/76 CRUD matrix** and offline write-safety tests remain its evidence. Safe rejection is a documented Alpha limitation, not counted as a successful query.

The offline suite for this safety snapshot passed **370/370** tests. The v2 corpus preflight passed with no provider calls. Report-integrity tests accept LF and CRLF encodings of identical frozen text, preserving the historical hashes while remaining portable across Git checkouts.

[Curated per-case outcomes and usage](alpha-safety-result.json) omit raw provider exchanges, runtime paths and credentials. The 24 retained live checks used **131 calls**, **110,436 input / 16,904 output tokens** (**127,340 total**), an estimated **$0.004638**, **2.17s median** and **5.09s p95** latency. These totals replace the earlier cardinality probe with its final retest; they are not the cost of a full Spider run or a repeatability claim.

Provider/model identity and run date are **not recorded in the curated result**. The usage above describes only these retained checks; it does not establish equivalent endpoint behavior.

## Reference-data caveat

The historical hometown case's request names `Little Lever Urban District`, and SQLite stores that exact title-case value. Spider's gold SQL instead compares `little lever urban district`. With SQLite's default BINARY text comparison:

```sql
-- Request-grounded exclusion, used by IntentSQL:
WHERE hometown != 'Little Lever Urban District'
-- Historical Spider gold; different under case-sensitive comparison:
WHERE hometown != 'little lever urban district'
```

Those predicates return different rows. The original incorrect-result classification is preserved in the historical report; the targeted check records this discrepancy rather than changing the gold, silently changing the operand's case, or claiming a new benchmark pass.

## Reproduction and limitations

Install `requirements-dev.txt`, then run:

```bash
python -m unittest discover -s tests -p "test*.py"
python benchmarks/v2/run_benchmark.py --preflight-only
python tools/release_validate.py --output ../intentsql-artifacts/release-smoke.json
```

Only the last command needs a running local server and configured provider. It uses an isolated fixture for mutation preview, commit and undo. The offline safety regressions are in `tests/test_alpha_safety.py`; Spider manifests and reference SQL remain frozen.

No observed wrong-execution defect remains reproduced by these targeted checks. This is **not a guarantee of correct interpretation for arbitrary English**: meaning and coverage still depend on probabilistic decisions. Inspect important results and all mutation previews. Ambiguous meanings, unsupported expressions/joins and some supported phrasings can reject safely.
