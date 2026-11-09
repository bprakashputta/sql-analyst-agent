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
- **Evaluated on BIRD mini-dev** (execution accuracy, 400 held-out questions), with a safety suite and a $5 hard spend cap enforced in code.

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
     llm.py: ChatAnthropic (Claude Haiku 5.5), JSON-schema output, versioned prompts,
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

EVAL_SECTION

## Design decisions

DESIGN_SECTION

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

LIMITS_SECTION

## Data and licenses

- **BIRD mini-dev** (Li et al., *Can LLM Already Serve as A Database Interface? A BIg Bench for Large-Scale Database Grounded Text-to-SQLs*), CC BY-SA 4.0. Questions from the 2026 revision on Hugging Face (`birdsql/bird_mini_dev`), databases from the official mini-dev archive. Downloaded by `make data` into the gitignored `data/`; only question ids and model outputs are committed.
- **Chinook** sample database, MIT, (c) 2008-2024 Luis Rocha, pinned to a commit and checked by SHA-256.

## License

MIT
