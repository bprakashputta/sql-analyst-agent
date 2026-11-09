# 006: SQLAlchemy over SQLite and Postgres; Chinook on Postgres for the demo, BIRD on SQLite for the eval

- **Status:** accepted
- **Date:** 2026-11-08

## Context
Clients' data is usually in Postgres (or another server database); the public text-to-SQL benchmark with
the most realistic questions, BIRD, ships SQLite files.

## Decision
- One `Database` class on SQLAlchemy engines: SQLite (`sqlite:///path`) and Postgres
  (`postgresql://...`, psycopg 3). Introspection uses SQLAlchemy's inspector for both; the agent and the
  guard get the dialect name and adapt (prompt notes, sqlglot dialect).
- **docker compose** runs **Postgres 17 with Chinook** (a sample digital-music store: customers,
  invoices, tracks, employees; MIT licensed), loaded from a pinned commit with a checksum at image build,
  and the app connects as the read-only role. One command brings up a realistic deployment.
- **Local quick start and the safety suite** use the Chinook SQLite file (same data, PascalCase names).
- **The eval** runs on BIRD mini-dev's SQLite databases (ADR 004).

## Alternatives considered
- **SQLite only:** simpler, but hides the server-side controls (roles, statement_timeout) that matter
  in a real deployment.
- **BIRD's Postgres version for the eval:** the 2026 release has Postgres questions too, but loading 11
  databases (1.4 GB) into Postgres would slow every reproduction; the agent code path is the same.

## Consequences
Two layer-2 implementations to maintain (SQLite authorizer, Postgres read-only transaction). The
Postgres path is tested in CI against the real compose image (`tests/test_postgres.py`).
