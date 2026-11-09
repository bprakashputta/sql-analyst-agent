import hashlib
import sqlite3

import pytest

from sql_analyst.db import Database, QueryError, QueryTimeout


def test_select_returns_rows(shop_db):
    res = shop_db.run("SELECT name FROM customers ORDER BY id", max_rows=10)
    assert res.columns == ["name"]
    assert res.rows == [["Ana"], ["Ben"], ["Chen"]]
    assert not res.truncated


def test_max_rows_truncates(shop_db):
    res = shop_db.run("SELECT * FROM order_items", max_rows=2)
    assert res.row_count == 2 and res.truncated


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM customers",
        "UPDATE products SET price = 0",
        "INSERT INTO customers VALUES (9, 'x', 'y')",
        "DROP TABLE orders",
        "CREATE TABLE t (x)",
        "PRAGMA query_only = 0",
        "ATTACH DATABASE ':memory:' AS m",
        "SELECT load_extension('x')",
        "BEGIN IMMEDIATE",
    ],
)
def test_connection_blocks_writes_without_the_guard(shop_db, shop_path, sql):
    before = hashlib.sha256(shop_path.read_bytes()).hexdigest()
    with pytest.raises((QueryError, sqlite3.Error)):
        shop_db.run(sql, max_rows=5)
    assert hashlib.sha256(shop_path.read_bytes()).hexdigest() == before
    assert shop_db.run("SELECT COUNT(*) FROM customers", max_rows=1).rows == [[3]]


def test_timeout_stops_a_runaway_query(shop_path):
    db = Database.sqlite(shop_path, timeout_ms=200)
    runaway = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT MAX(x) FROM n"
    with pytest.raises(QueryTimeout):
        db.run(runaway, max_rows=1)
    assert db.run("SELECT 1", max_rows=1).rows == [[1]]  # the connection is still usable


def test_from_url_sqlite(shop_path):
    db = Database.from_url(f"sqlite:///{shop_path}")
    assert db.dialect == "sqlite" and db.ping()


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        Database.sqlite(tmp_path / "nope.sqlite")
