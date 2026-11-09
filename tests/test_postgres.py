"""Postgres layer, against the docker compose database (CI job `postgres`). Skipped without PG_TEST_URL.

These run the hostile SQL straight at the read-only role, with the guard bypassed, to show the
database layer holds on its own.
"""

import os

import psycopg
import pytest

from sql_analyst.db import Database, QueryError, QueryTimeout
from sql_analyst.guard import check_sql
from sql_analyst.schema import introspect

URL = os.environ.get("PG_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PG_TEST_URL not set")

WRITES = [
    "DELETE FROM customer",
    "UPDATE track SET unit_price = 0",
    "INSERT INTO genre (genre_id, name) VALUES (999, 'x')",
    "DROP TABLE invoice",
    "CREATE TABLE pwned (x int)",
    "CREATE TEMP TABLE pwned (x int)",
    "TRUNCATE invoice_line",
    "ALTER TABLE customer ADD COLUMN x text",
    "GRANT ALL ON customer TO PUBLIC",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * INTO backup FROM customer",
]


@pytest.fixture(scope="module")
def db():
    return Database.from_url(URL, timeout_ms=500)


def test_reads_work(db):
    res = db.run("SELECT COUNT(*) FROM customer", max_rows=1)
    assert res.rows == [[59]]
    assert db.dialect == "postgres"


def test_introspection(db):
    schema = introspect(db.meta_engine, "postgres")
    assert {"invoice", "customer", "track"} <= set(schema.tables)
    assert any(fk.ref_table == "customer" for fk in schema.tables["invoice"].foreign_keys)


@pytest.mark.parametrize("sql", WRITES)
def test_connection_refuses_writes_without_guard(db, sql):
    with pytest.raises((QueryError, QueryTimeout)):
        db.run(sql, max_rows=1)
    assert db.run("SELECT COUNT(*) FROM customer", max_rows=1).rows == [[59]]


@pytest.mark.parametrize("sql", WRITES)
def test_guard_refuses_them_too(sql):
    assert not check_sql(sql, dialect="postgres", allowed_tables=None, row_limit=10).ok


def test_role_cannot_write_even_outside_a_read_only_transaction():
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM genre WHERE genre_id = 1")


def test_statement_timeout(db):
    with pytest.raises(QueryTimeout):
        db.run("SELECT pg_sleep(3)", max_rows=1)


def test_percent_sign_is_not_a_placeholder(db):
    res = db.run("SELECT COUNT(*) FROM track WHERE name LIKE '%Love%'", max_rows=1)
    assert res.rows[0][0] > 0
