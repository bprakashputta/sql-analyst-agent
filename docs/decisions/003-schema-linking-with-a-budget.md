# 003: Schema linking with a value index, used only past a token budget

- **Status:** accepted
- **Date:** 2026-11-08

## Context
Dumping every table into the prompt doesn't scale to a warehouse with hundreds of tables: it costs
tokens on every call and buries the relevant columns. But a linker that leaves out a needed table
makes the question unanswerable, which is worse than a long prompt.

## Decision
`schema.link()` scores tables lexically against the question and BIRD-style hints:
- column and table name tokens (camelCase / snake_case split, light plural stemming);
- a **value index**: for low-cardinality text columns (≤1,000 distinct values in the first 50k rows)
  every value is indexed for matching only (never put in the prompt), so "Marvel Comics" or "Italian
  Grand Prix" in a question pulls in the table that holds it;
- the top 6 tables, then the **bridge tables** on the foreign-key path between them, so joins are
  possible.

The card sent to the model carries types, keys, foreign keys and up to 4 frequent sample values per
text column.

Linking runs **only when the full schema card exceeds 3,000 tokens** (`link_token_budget`). On a
linked prompt the agent **widens to the full schema** when a query fails on a missing table or column,
returns nothing, or when the model asks for clarification (it may be missing a table, not confused).

## Evidence (dev split, N=100, unless marked test)
Table recall = share of questions where every table the gold query uses was linked:

| linker version | recall |
|---|---|
| names only, top 4, drop tables under 25% of the best score | 76% |
| + value index | 82% |
| + top 6, no cutoff, plural stemming | **94%** |

LINK_TEST_EVIDENCE

## Alternatives considered
- **Embedding retrieval over columns:** another service and model call per question; the misses we saw
  were value mentions and bridge tables, which the value index and FK walk handle directly.
- **LLM table selection:** an extra call per question for every question, including the small
  schemas where the whole card fits anyway.
- **Always link:** the measured cost (above) for a small token saving on small schemas.

## Consequences
On every BIRD database (3-13 tables, 210-2,300 tokens with samples) the production agent sends the
full card, so the headline numbers exercise self-correction, not linking; the `agent_forced_linking`
run is the measurement of the linker itself. Lexical linking can't map synonyms ("revenue" to
`Invoice.Total`): on the Chinook demo it linked `Album` for "Monthly revenue in 2023". The widening
fallbacks exist for exactly that.
