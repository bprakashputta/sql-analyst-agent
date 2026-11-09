#!/bin/sh
# The role the agent connects as. It can read the chinook tables and nothing else:
#   - SELECT on existing tables only; no INSERT/UPDATE/DELETE/TRUNCATE, no CREATE, no TEMP tables
#   - every transaction read-only by default, and a 5 s statement timeout
# The app additionally wraps each query in BEGIN READ ONLY + SET LOCAL statement_timeout (db.py).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname chinook <<SQL
CREATE ROLE analyst_ro LOGIN PASSWORD '${ANALYST_RO_PASSWORD:-analyst_ro}' CONNECTION LIMIT 20;
ALTER ROLE analyst_ro SET default_transaction_read_only = on;
ALTER ROLE analyst_ro SET statement_timeout = '5s';
ALTER ROLE analyst_ro SET idle_in_transaction_session_timeout = '10s';
REVOKE ALL ON DATABASE chinook FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE chinook TO analyst_ro;
GRANT USAGE ON SCHEMA public TO analyst_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_ro;
SQL
