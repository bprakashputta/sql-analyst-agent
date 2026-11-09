# 004: Evaluate on BIRD mini-dev with execution accuracy, a fixed dev/test split and paired intervals

- **Status:** accepted
- **Date:** 2026-11-08

## Context
"It answered my five demo questions" says nothing about how often a text-to-SQL agent is right. We need
a public benchmark with real databases, a held-out answer key, and a metric that checks results, not
SQL text (many different queries are correct).

## Decision
- **Dataset:** BIRD mini-dev, SQLite, the 2026 revision (500 questions over 11 databases, 1.4 GB;
  CC BY-SA 4.0). It is hard: 460 of the 500 questions are labelled *challenging*. The hints BIRD
  provides ("evidence") are given to every configuration, as in the standard BIRD setting.
- **Split:** the 500 ids shuffled with seed 20261108: **dev = 100** (all prompt, linker and
  effort decisions were made here) and **test = 400** (run once per configuration for the report).
  `evals/split.json` is committed.
- **Metric:** execution accuracy, BIRD's set comparison of result rows (`ex_set`), floats rounded to
  6 dp. Also reported: the same without rounding (BIRD's raw scorer) and an order- and
  duplicate-sensitive variant (`ex_ordered`, list comparison when the gold query ends in ORDER BY).
- **Uncertainty:** 95% bootstrap intervals (2,000 resamples) on accuracy, and *paired* bootstrap
  intervals on differences between configurations over the same questions. A difference whose
  interval includes zero is reported as "no measurable difference".
- **Honest cost and latency:** test runs disable the response cache (`LLM_CACHE=false`), so every
  question pays and waits for its own model calls.
- **What is scored:** the final SQL the agent executed, re-run in a fresh read-only connection with a
  30 s timeout and a 100,000-row cap. The first attempt is scored too, so self-correction's rescues
  and regressions are counted, not assumed.

## Alternatives considered
- **Spider dev:** easier and saturated (frontier models are well above 80%), so it separates
  configurations less; its databases are toy-sized.
- **Exact-match / component-match on SQL text:** penalizes correct alternatives.
- **The whole 500 as test:** leaves nothing to iterate on without peeking.

## Consequences
At N=400 a 95% interval is about ±4.7 pp wide around 60-70%, so improvements smaller than ~5 pp can't
be claimed from one run. Dev decisions were made at N=100, where run-to-run noise alone moved the same
configuration by 3 pp (68% and 71% for two functionally identical configs).
