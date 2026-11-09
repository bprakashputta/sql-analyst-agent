# 002: Parse and re-render every query with sqlglot

- **Status:** accepted
- **Date:** 2026-11-08

## Context
The guard has to understand SQL structurally (a DELETE inside a CTE, a second statement after a comment)
and it has to add a LIMIT, without changing what the query means.

## Decision
`sqlglot.parse(sql, read=dialect)`; refuse anything that doesn't parse, isn't exactly one statement, or
whose root isn't a SELECT / UNION / INTERSECT / EXCEPT. Walk the whole tree for forbidden node types
(Insert, Update, Delete, Merge, Create, Drop, Alter, Command, Into, Lock, Pragma, Attach, Copy, Set, ...)
and deny-listed functions, check every table against the introspected allowlist (CTE names excepted),
then set or cap the outer LIMIT and run `tree.sql(dialect)`, the re-rendered query, not the original
text.

Re-rendering means the database runs exactly what the guard analysed: there is no gap between "what we
checked" and "what ran".

## Evidence
- **No false refusals on real analytical SQL:** the guard accepts all **500/500** BIRD mini-dev gold
  queries (CTEs, window functions, correlated subqueries, set operations).
- **Fidelity:** re-rendering changed the result of **0/500** gold queries (`python -m
  sql_analyst.evals.fidelity`, exact ordered comparison, `evals/guard_fidelity.json`).
- **Cost:** 2-12 ms per query in the step traces of the demo app.

## Alternatives considered
- **sqlparse:** a tokenizer, not a parser: no reliable tree to walk, can't re-render.
- **EXPLAIN-based checks:** dialect-specific, and EXPLAIN itself has to be allowed.
- **Running the original text after a check:** leaves a parse-differential gap.

## Consequences
Queries sqlglot can't parse are refused even if the database would accept them. That is the safe
direction; the self-correction loop gives the model the parse error and another try.
