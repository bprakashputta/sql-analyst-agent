# sql-analyst-agent

[![CI](https://github.com/bprakashputta/sql-analyst-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/bprakashputta/sql-analyst-agent/actions/workflows/ci.yml)

**Chat with your database, safely: a LangGraph agent that answers business questions in SQL, shows the query it ran, charts the result and explains it, and cannot change your data.**

Ask *"Which five countries bring in the most revenue?"* and you get the answer in a sentence, the SQL that ran, the result table, a chart picked from the shape of the result, and every step the agent took (with tokens, cost and latency). Ask it to *"drop the invoice table"* and nothing reaches the database.

![The UI answering a revenue question: answer, bar chart, SQL panel and step trace](docs/screenshot.png)

- **Read-only by construction, three layers deep.** sqlglot parses every model-written query and allows one SELECT only; the connection is read-only (SQLite authorizer / Postgres `READ ONLY` transaction); the Postgres role has SELECT grants only. Each layer blocks writes on its own ([ADR 001](docs/decisions/001-read-only-by-construction.md)).
- **Statement timeouts and row caps** on every query, so a runaway join can't hang the service.
- **Self-correction loop:** a database error, a guard refusal, a timeout or an empty result goes back to the model with the error, up to 3 attempts, widening to the full schema when the error names a missing table.
- **Schema linking** with a value index (finds `publisher` from "Marvel Comics" in the question), used when the schema is too big to send whole.
- **Clarifies instead of guessing** when a question is ambiguous ("best customers: by revenue or by orders?"), and declines write requests with a reason instead of retrying.
- **Checked explanations:** every number in the written answer is matched against the result rows; unmatched numbers get one rewrite, then a visible warning.
- **Evaluated on BIRD mini-dev** (execution accuracy on held-out questions): sample values + self-correction lift Haiku 5.5 from 62.7% to 71.8% (N=400, +9.0 pp paired), and the default Sonnet 5.5 reaches 87% (N=100). Safety suite: 0 non-SELECT statements executed.

```
 browser ── POST /api/ask ──►  FastAPI (api.py)
                                  │
                                  ▼
 ┌──────────────────────────── LangGraph agent (agent.py) ─────────────────────────────┐
 │ link_schema ─► generate_sql ─► guard ─► execute ─► chart ─► explain ─► answer      │
 │    │              │  ▲   │       │         │                    │                   │
 │ schema.py         │  │   │   guard.py   db.py              verify.py               │
 │ (FKs, samples,    │  └ retry ◄─ refused / error / timeout / empty (≤3 attempts,     │
 │  value index)     │          widen to full schema on "no such table/column")      │
 │                   ├─► clarify (ambiguous question: ask, run nothing)               │
 │                   └─► declined (write request: say why, run nothing)               │
 └───────────────────┼───────────────────────────────────────────────────────────────┘
                     ▼
     llm.py: ChatAnthropic (Sonnet 5.5 default, Haiku 5.5 budget), JSON-schema output, versioned prompts,
             spend ledger + hard cap, response cache, per-call tokens/cost/latency log
                                  │
     db.py ──► SQLite (mode=ro + authorizer) │ Postgres (analyst_ro role, READ ONLY txn)
```

## Quick start

### Docker (Postgres + Chinook sample store)

```bash
git clone https://github.com/bprakashputta/sql-analyst-agent && cd sql-analyst-agent
cp .env.example .env            # add ANTHROPIC_API_KEY
docker compose up --build       # Postgres 17 with Chinook + the app, read-only role
open http://localhost:8000
```

### Local (SQLite)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run sql-analyst demo-data    # Chinook SQLite into data/demo/
uv run sql-analyst serve        # http://127.0.0.1:8000
uv run sql-analyst ask "Average invoice total by sales support agent"
```

Point it at your own database with `DATABASE_URL` (`sqlite:///path.db` or `postgresql://user:pass@host/db`). Use a read-only role for Postgres: `docker/postgres/10-readonly-role.sh` shows the grants.

### API

| Endpoint | |
|---|---|
| `POST /api/ask` `{question, clarification?}` | status (`answered` / `clarify` / `declined` / `failed`), answer, caveats, SQL, columns, rows, chart spec, attempts, step trace, tokens and cost |
| `GET /api/schema` | tables, columns, row counts the agent can see |
| `GET /api/spend` | spend so far and the cap |
| `GET /health`, `GET /ready` | liveness; readiness (database answers, key configured) |

## Evaluation

**Dataset:** [BIRD mini-dev](https://huggingface.co/datasets/birdsql/bird_mini_dev) (SQLite, 2026 revision, CC BY-SA 4.0): 500 questions over 11 real databases (1.4 GB), 460 of them labelled *challenging*. BIRD's hints ("evidence") are given to every configuration. The 500 ids are split once with a fixed seed: **dev = 100** questions (every prompt, linker, effort and review decision was made here; only the final model default was chosen from the test-split comparison below) and **test = 400** (each configuration run once for this table). [ADR 004](docs/decisions/004-evaluation-on-bird.md) has the method.

**Metric:** execution accuracy (EX): the agent's final SQL and the gold SQL are both executed and their result rows compared as sets (BIRD's comparison; floats rounded to 6 dp; the raw-float and order-sensitive variants are in [`evals/results.md`](evals/results.md)). 95% intervals are bootstrap; differences are *paired* on the same questions. Test runs disable the response cache, so cost and latency are real.

| configuration (test split) | model | N | EX | 95% CI | $ / question | p50 / p95 latency |
|---|---|---|---|---|---|---|
| baseline: one shot, full schema, no sample values, no retries | Haiku 5.5 | 400 | 62.7% | 58.0-67.2 | $0.00058 | 4.4 s / 10.5 s |
| + sample values + self-correction (full schema) | Haiku 5.5 | 400 | 73.2% | 68.8-77.5 | $0.00065 | 4.5 s / 11.8 s |
| **agent** (production pipeline: BIRD schemas fit the budget, so sent whole) | Haiku 5.5 | 400 | **71.8%** | 67.2-76.2 | $0.00068 | 4.4 s / 14.8 s |
| agent with schema linking forced on | Haiku 5.5 | 400 | 66.2% | 61.8-70.8 | $0.00079 | 4.7 s / 14.3 s |
| **agent** | **Sonnet 5.5** | 100 | **87.0%** | 80.0-93.0 | $0.0063 | 2.8 s / 8.1 s |

Paired differences on the same questions:

| comparison | N | difference | 95% CI |
|---|---|---|---|
| agent vs baseline (Haiku) | 400 | **+9.0 pp** | +4.0 to +14.0 |
| full schema + self-correction vs baseline (Haiku) | 400 | +10.5 pp | +5.8 to +15.2 |
| linking forced vs agent (Haiku) | 400 | **-5.5 pp** | -10.0 to -1.0 |
| Sonnet 5.5 vs Haiku 5.5 (agent, same 100 questions) | 100 | **+18.0 pp** (87% vs 69%) | +8.0 to +28.0 |

What the numbers say:

- **Self-correction works and doesn't break things.** On test the Haiku agent retried 31 questions and turned 20 failing first attempts into correct answers (12 after an empty result, 5 after a timeout, 3 after a guard refusal), with **0** correct first attempts made wrong. Queries ending in an execution error dropped from 6 (baseline) to 2.
- **The agent and the full-schema configuration are the same pipeline on BIRD** (every BIRD schema is under the linking budget), so their 1.5 pp gap (95% CI -5.2 to +2.2) is run-to-run noise. That's the noise floor to keep in mind for any single-run comparison.
- **Schema linking costs accuracy when the schema fits.** Forcing it saved 12% of prompt tokens and cost 5.5 points: when the linker missed a needed table (43 of 400 questions), accuracy on those fell to 12%. Hence linking only above a 3,000-token schema ([ADR 003](docs/decisions/003-schema-linking-with-a-budget.md)).
- **Model choice is the biggest lever.** Sonnet 5.5 was 18 points better than Haiku on the same questions, *faster* (it wrote a third of the output tokens), at $0.006 per question. It is the default; Haiku is the budget mode ([ADR 005](docs/decisions/005-model-effort-and-result-review.md)).
- **What's left is interpretation, not syntax.** 103 of Haiku's 113 wrong test answers ran cleanly and returned a plausible, different result (distinct by name vs by id, which status counts as "active"). A model review pass of each result was tried on dev and gave no net gain for double the cost.

**Safety suite** (`make safety`, [`evals/safety.json`](evals/safety.json)):

| check | result |
|---|---|
| 45 hostile SQL strings (stacked statements, writes in CTEs, PRAGMA, ATTACH, VACUUM INTO, upserts, `load_extension`, catalog reads, NUL bytes, full-width semicolons) through the guard | **45/45 refused** |
| the same 45 sent straight to the read-only SQLite connection, guard bypassed | **37/37 writes refused** by the connection alone; database file SHA-256 unchanged |
| 11 writes sent straight to the Postgres `analyst_ro` connection, guard bypassed (`tests/test_postgres.py`, CI) | **11/11 refused**; the role also can't write after switching its read-only default off |
| 25 hostile questions and prompt injections through the full agent | **0 non-SELECT statements executed** (0 statements at all: all 25 declined with a reason) |
| 6 harmless questions with scary words ("tracks with 'Drop' in the name") | 6/6 answered, none falsely declined |
| guard on all 500 BIRD gold queries | 500/500 accepted; re-rendering changed **0/500** results ([guard fidelity](evals/guard_fidelity.json)) |

Total API spend for the whole project (development, dev-split iteration, all test runs, safety suite, screenshots): **$2.53** of the $5.00 cap, from the spend ledger.

## Design decisions

- **Three independent read-only layers** ([ADR 001](docs/decisions/001-read-only-by-construction.md)). The prompt asks for SELECT, but nothing relies on it: the guard, the connection and the database role each stop writes on their own, and the safety suite tests each layer with the others removed.
- **The guard re-renders what it checked** ([ADR 002](docs/decisions/002-sqlglot-guard.md)). The database runs sqlglot's re-rendered query, not the model's text, so there's no gap between what was analysed and what ran; the LIMIT is added on the tree. Unknown tables (including system catalogs) are refused against the introspected allowlist.
- **Retry on signals, not on vibes.** The loop retries on a DB error, a guard refusal, a timeout, or an empty / all-NULL result, feeds the exact error back, stops at 3 attempts, and accepts an empty result if the model returns the same query twice. A write request is *declined* with a reason instead of retried: retrying would only pressure the model into writing something.
- **Schema linking only when it pays** ([ADR 003](docs/decisions/003-schema-linking-with-a-budget.md)). Lexical scoring + a value index + FK bridge tables; used past 3,000 tokens of schema, with widening to the full schema on failure or on a clarification request.
- **Charts by rules, not by the model.** `chart.py` picks number / line / bar / grouped bar / scatter / table-only from the result's shape (time-like column, one label + measures, two measures) and says why. Rendered as inline SVG with hover tooltips, a palette validated for color-vision deficiency in light and dark, and the table always available.
- **Code checks the model's facts.** Every number in the written answer is matched against the result cells (allowing rounding, percentages, thousands/millions); unmatched numbers get one rewrite, then a visible warning in the UI.
- **Cost controls in code.** Spend ledger shared by the API, CLI and eval, checked before every call against `SPEND_CAP_USD`; Anthropic prompt caching on the system block (instructions + schema) so retries read it at a tenth of the price; a response cache for identical requests; per-call tokens, cost and latency in structured logs and in every API response.

## Development

```bash
uv sync
uv run pytest            # offline: the model is faked, SQLite fixtures are built per test
uv run ruff check . && uv run ruff format --check .
make data                # BIRD mini-dev (~800 MB download) + Chinook into data/
make eval                # the reported test-split runs (~$1-2 of API calls)
make safety              # the safety suite
```

Prompts are versioned files in `src/sql_analyst/prompts/`; the version is part of the cache key and of every logged call. Every Claude call is appended to `data/spend.jsonl`; the client refuses a call that could take the total past `SPEND_CAP_USD`.

## Limitations

- **Accuracy is not 100%, and it can't tell you when it's wrong.** On BIRD's hard questions the default model (Sonnet 5.5) was right on 87% of a 100-question sample and Haiku 5.5 on 72% of 400. The SQL, the row count and the caveats are always shown so a human can check; treat it as an analyst's draft, not an oracle.
- The Sonnet result is one run on 100 questions (95% CI 80-93%); the Haiku results are one run each on 400. Same-pipeline reruns differed by 1.5-3 points.
- BIRD databases have 3-13 tables, so the headline runs send the whole schema; linking was measured on BIRD only in the forced configuration, where it hurt. The linker is lexical and misses synonyms ("revenue" for `Invoice.Total`); large schemas would need a better retriever.
- The safety suite proves the layers block writes and the listed escapes. It does not cover denial of service beyond the statement timeout and row cap (e.g. many concurrent heavy queries), or data the role is allowed to read but a user shouldn't see: row- and column-level permissions belong in the database role.
- Single-turn questions plus one clarification step; no conversation memory or follow-ups ("now split that by year").
- Dialects: SQLite and Postgres. Others work through SQLAlchemy in principle but need their own read-only connection layer.
- Evaluated in English on BIRD; no claims about other languages or domains.

## Data and licenses

- **BIRD mini-dev** (Li et al., *Can LLM Already Serve as A Database Interface? A BIg Bench for Large-Scale Database Grounded Text-to-SQLs*), CC BY-SA 4.0. Questions from the 2026 revision on Hugging Face (`birdsql/bird_mini_dev`), databases from the official mini-dev archive. Downloaded by `make data` into the gitignored `data/`; only question ids and model outputs are committed.
- **Chinook** sample database, MIT, (c) 2008-2024 Luis Rocha, pinned to a commit and checked by SHA-256.

## License

MIT
