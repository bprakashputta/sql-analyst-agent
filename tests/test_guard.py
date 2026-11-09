import pytest

from sql_analyst.evals.safety import ATTACKS
from sql_analyst.guard import check_sql, statement_kind

TABLES = frozenset(
    {
        "customers",
        "orders",
        "order_items",
        "products",
        "genre",
        "track",
        "customer",
        "invoice",
        "employee",
    }
)


def ok(sql: str, dialect: str = "sqlite", row_limit: int = 100):
    return check_sql(sql, dialect=dialect, allowed_tables=TABLES, row_limit=row_limit)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "select name, country from customers where country = 'Brazil' order by name",
        "WITH b AS (SELECT * FROM customers WHERE country='Brazil') SELECT COUNT(*) FROM b",
        "SELECT country FROM customers UNION SELECT title FROM products",
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY 1",
        "SELECT name, RANK() OVER (ORDER BY id) FROM customers",
        "SELECT * FROM (SELECT id FROM orders) AS t WHERE id IN (SELECT order_id FROM order_items)",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x < 5) SELECT x FROM n",
        "SELECT 'DROP TABLE customers; --' AS harmless_text",
        'SELECT "name" FROM "customers" -- trailing comment',
        "SELECT 1;",
    ],
)
def test_allows_read_queries(sql):
    res = ok(sql)
    assert res.ok, res.reason


@pytest.mark.parametrize("label,sql,_w", ATTACKS, ids=[a[0] for a in ATTACKS])
def test_refuses_every_attack(label, sql, _w):
    assert not ok(sql).ok


@pytest.mark.parametrize(
    "sql,why",
    [
        ("SELECT * FROM secrets", "unknown table"),
        ("SELECT * FROM sqlite_master", "system catalog"),
        ("SELECT * FROM main.sqlite_schema", "system catalog"),
        ("SELECT * FROM pg_catalog.pg_roles", "system catalog"),
        ("SELECT * FROM information_schema.columns", "system catalog"),
        ("SELECT * FROM pg_user", "system catalog"),
        ("SELECT pg_sleep(5)", "pg_sleep"),
        ("SELECT name FROM customers WHERE id = (SELECT load_extension('x'))", "load_extension"),
        ("", "empty"),
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("SELEC name FROM customers", "parse"),
    ],
)
def test_refusal_reasons(sql, why):
    res = ok(sql)
    assert not res.ok
    assert why in (res.reason or "")


def test_adds_limit_when_missing():
    res = ok("SELECT * FROM customers", row_limit=50)
    assert res.limit_applied == 50
    assert res.sql.endswith("LIMIT 50")


def test_keeps_smaller_limit_and_caps_larger():
    assert ok("SELECT * FROM customers LIMIT 5", row_limit=50).sql.endswith("LIMIT 5")
    assert ok("SELECT * FROM customers LIMIT 5000", row_limit=50).sql.endswith("LIMIT 50")


def test_limit_on_union_applies_to_whole_query():
    res = ok("SELECT name FROM customers UNION SELECT title FROM products", row_limit=10)
    assert res.ok and res.sql.endswith("LIMIT 10")


def test_cte_names_are_not_unknown_tables():
    assert ok("WITH big AS (SELECT * FROM orders WHERE total > 100) SELECT * FROM big").ok


def test_reports_tables_read():
    res = ok("SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id")
    assert set(res.tables) == {"customers", "orders"}


def test_postgres_dialect():
    assert ok("SELECT name FROM customers WHERE name ILIKE 'a%'", dialect="postgres").ok
    assert not ok("SELECT pg_read_file('/etc/passwd')", dialect="postgres").ok
    assert not ok("COPY customers TO PROGRAM 'id'", dialect="postgres").ok
    assert not ok("SET ROLE postgres", dialect="postgres").ok


def test_without_allowlist_any_table_is_ok():
    assert check_sql(
        "SELECT * FROM anything", dialect="sqlite", allowed_tables=None, row_limit=5
    ).ok


def test_statement_kind():
    assert statement_kind("SELECT 1", "sqlite") == "SELECT"
    assert statement_kind("DROP TABLE x", "sqlite") == "DROP"
    assert statement_kind("SELECT 1; SELECT 2", "sqlite") == "MULTI"
