# Sharing IntentSQL Alpha

Publication draft: the repository links below target the planned public URL.

Lead with the experiment, then show the application. Describe bounded decisions
and checks as things the viewer can inspect; avoid claims about a new kind of
intelligence. Do not use test counts or Spider results as the hook.

## Discord

I've been experimenting with composing small bounded Jev decisions inside
ordinary software, instead of treating each judgment as a one-shot classifier.

IntentSQL is the working example: SQLite's schema and code bound the choices;
Jev resolves meaning; later checks can expose incomplete plans. The result is
inspectable SQL—or a refusal. Curious where this composition works and fails.

[IntentSQL](https://github.com/Amine-LG/IntentSQL)

## X

Experiment: can small bounded Jev judgments compose into useful behavior?

IntentSQL: schema/code bound the choices, semantic decisions build a plan, checks
can reject it. Code compiles the SQL. SQLite is the test bed.

https://github.com/Amine-LG/IntentSQL

## LinkedIn

I've been exploring what happens when semantic judgment becomes a programmable
primitive inside otherwise deterministic software.

IntentSQL is a small, working Alpha: a natural-language SQLite interface with
inspectable decisions and guarded writes. The database and code supply real
columns, values, relationships and legal operations. Jev makes bounded
System One choices; later steps receive selected state, and coverage checks can
trigger specific reviews or reject an incomplete plan. Code compiles the SQL.

The question is whether this composition can navigate a constrained problem
usefully. SQL gives us observable failures and independent reference results. This is one
experiment, with real limits.

[Source and demo](https://github.com/Amine-LG/IntentSQL)

## Short demo: show the composition

The success screenshot records Jev / `jev-latest`; the curated smoke report does
not retain provider/model identity or raw call traces. Its final plan and refusal
are evidence, not a transcript to reconstruct. Capture a new live demo honestly.

Aim for about 45–60 seconds, with captions instead of a long introduction.

1. **Request → result.** On DESE, enter: “Show the five districts with the highest
   per-pupil expenditure. Return district name and per-pupil expenditure,
   highest first.” Capture an actual live run; keep its final SQL and rows visible.
2. **Expose the path.** Briefly open Semantic Decisions: show the source/relation
   and ordering decisions that actually occurred, then the coverage question. Show
   the evolving resolved-decision summary while it runs, or the final typed program
   afterward. Caption: “Later decisions receive selected context.” The summary
   is not a complete internal-state debugger.
3. **Show the boundary.** On Moneyball, reproduce recorded smoke case F16: “Return
   the two tallest players within each birth country, ranked separately inside every country.” Capture the
   actual refusal and its trace. Caption: “Unsupported shape: no answer query.”
   If a live run behaves differently, show it honestly; do not stage a refusal
   or substitute fabricated output.
4. **Close on the question.** “What can bounded semantic decisions do when code
   builds the possibilities and checks the result?” Link the repository.

The current app streams real calls and a selected-decision summary. It does not
visualize an exhaustive search tree or training process; do not animate one into
the video. Optional raw exchanges can establish provenance, but a dump of JSON
should not dominate the demo. A rejection is part of the experiment, not a
reason to edit around its failure.
