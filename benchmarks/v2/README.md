# IntentSQL Alpha benchmark v2

The v2 corpus keeps **all 80 v1 prompts and independent reference queries**. It changes only two stale capability assumptions:

- `REJ-07` moves from expected rejection to supported adversarial execution. One validated direct join can now carry predicates from both joined tables.
- The four `PARA-MIN` paraphrases accept either a one-row minimum or a typed tie-preserving global minimum. Both return the same rows on the frozen DESE database, whose minimum is unique. The evaluator checks the explicit `global_extremum` typed state rather than merely accepting a missing `LIMIT`.

The resulting suites contain 20 Core, 20 paraphrase, 21 adversarial, and 19 expected-rejection cases. No question or gold SQL was replaced. Historical [v1](../v1/README.md) remains unchanged.

Run `python benchmarks/v2/run_benchmark.py --preflight-only` before `python benchmarks/v2/run_benchmark.py --runs 1`. The runner checks typed semantics and independent SQLite execution, writes reports outside the repository under the sibling artifact directory reported by `benchmarks.artifacts.artifact_root()`, and records provider usage and latency. A single run is a measurement, not a stability estimate.

An initial v2 evaluator compared joined filter columns as single strings (`salaries.year`), while the typed program stores table and column separately. The live run preserved the correct SQL, typed plan, and rows. `regrade.py` checks every saved case against the current corpus and independent SQLite references, preserves the original report, and writes a clearly labeled derived report without another provider call.

The committed result identifies **Jev / `jev-latest`**. This internal development corpus does not establish accuracy for other compatible endpoints.

The recorded release-preparation compiler passed **80/80** in one run: Core 20/20, paraphrase 20/20, adversarial 21/21, and safe rejection 19/19. It used 588 calls, 597,603 total tokens, an estimated $0.022013, 2.56s median latency, and 4.58s p95. The [per-case public result](result-alpha.json) omits raw provider exchanges. This one run does not establish repeatability. The full live suite was not repeated after the final [Alpha safety fixes](../ALPHA_SAFETY.md); its recorded compiler commit identifies the measured version.
