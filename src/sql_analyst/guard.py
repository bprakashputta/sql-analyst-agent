"""SQL guard: decide whether a model-written query may run, before it touches the database.

The guard parses the query with sqlglot and allows exactly one read-only statement:

1. It must parse in the target dialect. Anything sqlglot can't parse is refused, not passed through.
2. It must be a single statement (`SELECT 1; DROP TABLE t` is two).
3. The root must be a SELECT or a set operation (UNION / INTERSECT / EXCEPT) of SELECTs, optionally with
   a WITH clause. PRAGMA, ATTACH, COPY, EXPLAIN, VACUUM, SET and everything sqlglot parses as an opaque
   `Command` are refused.
4. No write or side-effect node may appear anywhere in the tree: a DELETE inside a CTE, SELECT ... INTO,
   SELECT ... FOR UPDATE, and so on.
5. No function on the deny list (pg_sleep, load_extension, pg_read_file, dblink, ...).
6. Every table it reads must be a known table of this database (or a CTE it defines). System catalogs
   (sqlite_master, pg_catalog, information_schema) are not on that list.
7. The outermost query gets a LIMIT no larger than `row_limit`.

The guard is the first of three layers. The connection is also read-only (SQLite `mode=ro` +
`query_only` + an authorizer; Postgres read-only role + read-only transaction) and has a statement
timeout, so a query that slips past this parser still can't write or run forever. See ADR 002.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Nodes that write, change session state or leave the SELECT world. Checked anywhere in the tree.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "TruncateTable",
        "Command",
        "Into",
        "Lock",
        "Transaction",
        "Commit",
        "Rollback",
        "Set",
        "Pragma",
        "Attach",
        "Detach",
        "Copy",
        "Grant",
        "Use",
        "Analyze",
        "Kill",
        "Describe",
        "Show",
        "Cache",
        "Uncache",
        "Refresh",
        "LoadData",
        "Execute",
    )
    if hasattr(exp, name)
)

# Functions that read files, sleep, load code, reach other servers or change server state.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        # SQLite
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "zipfile",
        "sqlite_compileoption_get",
        # Postgres
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_reload_conf",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "lo_import",
        "lo_export",
        "lo_get",
        "lo_put",
        "dblink",
        "dblink_exec",
        "set_config",
        "query_to_xml",
        "query_to_json",
        "txid_current",
        "nextval",
        "setval",
        # MySQL / generic
        "sleep",
        "benchmark",
        "load_file",
    }
)

_SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "sqlite_master", "sqlite_temp"})


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    sql: str | None = None
    reason: str | None = None
    tables: tuple[str, ...] = field(default_factory=tuple)
    limit_applied: int | None = None


def _refuse(reason: str) -> GuardResult:
    return GuardResult(ok=False, reason=reason)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return node.sql_name().lower()


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _apply_limit(tree: exp.Expression, row_limit: int) -> tuple[exp.Expression, int | None]:
    """Cap the outermost query at row_limit rows. Returns the tree and the cap if one was set."""
    limit = tree.args.get("limit")
    if limit is not None:
        value = limit.expression if isinstance(limit, exp.Limit) else None
        if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= row_limit:
            return tree, None
        # A LIMIT expression we can't read (a parameter, a subquery) or one above the cap: replace it.
    if isinstance(tree, exp.Select | exp.SetOperation):
        return tree.limit(row_limit, copy=False), row_limit
    return tree, None


def check_sql(
    sql: str,
    *,
    dialect: str,
    allowed_tables: set[str] | frozenset[str] | None,
    row_limit: int,
) -> GuardResult:
    """Validate one model-written query. `allowed_tables` are lower-case names; None skips that check."""
    text = (sql or "").strip()
    if not text:
        return _refuse("empty query")
    if "\x00" in text:
        return _refuse("query contains a NUL byte")
    try:
        statements = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except (ParseError, TokenError) as e:
        return _refuse(f"could not parse the query as {dialect}: {str(e).splitlines()[0][:200]}")
    if len(statements) != 1:
        return _refuse(f"expected exactly one statement, got {len(statements)}")
    tree = statements[0]

    if not isinstance(tree, exp.Select | exp.SetOperation):
        return _refuse(f"only SELECT queries are allowed, got {type(tree).__name__.upper()}")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return _refuse(f"{type(node).__name__.upper()} is not allowed in a read-only query")
        if isinstance(node, exp.Func) and _function_name(node) in FORBIDDEN_FUNCTIONS:
            return _refuse(f"function {_function_name(node)}() is not allowed")

    ctes = _cte_names(tree)
    tables: list[str] = []
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        schema = (table.db or "").lower()
        if not name:  # table-valued function such as json_each(...): already vetted as a Func
            continue
        if schema in _SYSTEM_SCHEMAS or name.startswith(("sqlite_", "pg_")):
            return _refuse(f"system catalog {table.sql()} is not readable")
        if name in ctes and not schema:
            continue
        if allowed_tables is not None and name not in allowed_tables:
            return _refuse(f"unknown table {table.name!r}")
        tables.append(name)

    tree, limit_applied = _apply_limit(tree, row_limit)
    return GuardResult(
        ok=True,
        sql=tree.sql(dialect=dialect),
        tables=tuple(dict.fromkeys(tables)),
        limit_applied=limit_applied,
    )


def statement_kind(sql: str, dialect: str) -> str:
    """The root statement type of a query, for the safety report (`SELECT`, `DROP`, `UNPARSEABLE`...)."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except (ParseError, TokenError):
        return "UNPARSEABLE"
    if not statements:
        return "EMPTY"
    if len(statements) != 1:
        return "MULTI"
    return type(statements[0]).__name__.upper()
