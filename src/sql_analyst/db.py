"""Read-only database access for SQLite and Postgres through SQLAlchemy.

This is the second and third safety layer under the SQL guard (guard.py):

SQLite
  - opened with a `file:...?mode=ro` URI, so the file handle itself can't write;
  - `PRAGMA query_only = ON` on every connection;
  - an authorizer callback (sqlite3's engine-level hook), installed around each model-written statement,
    that allows only reads, SELECT, recursive CTEs and functions not on the deny list. ATTACH, PRAGMA,
    writes and transactions are denied by the engine itself;
  - a progress handler that aborts a statement after `timeout_ms`.

Postgres
  - the app connects as a role that only has SELECT grants (docker/postgres/10-readonly-role.sql);
  - every query runs in `BEGIN READ ONLY` with `SET LOCAL statement_timeout`, and the transaction is
    always rolled back.

Rows are fetched with fetchmany(max_rows + 1), so a huge result can't exhaust memory even if the LIMIT
the guard added were somehow missing.
"""

from __future__ import annotations

import datetime as dt
import decimal
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .guard import FORBIDDEN_FUNCTIONS


class QueryTimeout(Exception):
    pass


class QueryError(Exception):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: float
    row_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.row_count = len(self.rows)


_SQLITE_ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


def _sqlite_authorizer(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
    if action not in _SQLITE_ALLOWED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FORBIDDEN_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and (arg1 or "").lower() in ("sqlite_master", "sqlite_schema"):
        # Reading the schema table is how SQLite itself resolves names, so it can't be denied here;
        # the guard refuses queries that name it.
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_OK


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    return value


class Database:
    """One database the agent may read. Create it with `Database.from_url`."""

    def __init__(
        self, engine: Engine, *, timeout_ms: int, meta_engine: Engine | None = None
    ) -> None:
        self.engine = engine
        # Schema introspection runs trusted, fixed queries (PRAGMA table_info on SQLite), which the
        # authorizer on `engine` denies. It gets its own read-only engine without the authorizer.
        self.meta_engine = meta_engine or engine
        self.timeout_ms = timeout_ms

    @property
    def dialect(self) -> str:
        name = self.engine.dialect.name
        return "postgres" if name == "postgresql" else name

    @classmethod
    def from_url(cls, url: str, *, timeout_ms: int = 5000) -> Database:
        if url.startswith("sqlite"):
            path = url.split("///", 1)[1] if "///" in url else url.removeprefix("sqlite:")
            return cls.sqlite(Path(path), timeout_ms=timeout_ms)
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgres://")
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
        engine = sa.create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        return cls(engine, timeout_ms=timeout_ms)

    @classmethod
    def sqlite(cls, path: Path, *, timeout_ms: int = 5000) -> Database:
        resolved = Path(path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"SQLite database not found: {resolved}")

        def connect() -> sqlite3.Connection:
            conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only = ON")
            return conn

        def connect_meta() -> sqlite3.Connection:
            conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only = ON")
            return conn

        engine = sa.create_engine("sqlite://", creator=connect, poolclass=sa.pool.QueuePool)
        meta = sa.create_engine("sqlite://", creator=connect_meta, poolclass=sa.pool.StaticPool)
        return cls(engine, timeout_ms=timeout_ms, meta_engine=meta)

    # -- execution -------------------------------------------------------------------------------

    def run(self, sql: str, *, max_rows: int) -> QueryResult:
        """Run one already-guarded SELECT and return at most max_rows rows."""
        start = time.perf_counter()
        try:
            if self.engine.dialect.name == "sqlite":
                columns, rows, truncated = self._run_sqlite(sql, max_rows)
            else:
                columns, rows, truncated = self._run_postgres(sql, max_rows)
        except QueryTimeout:
            raise
        except sa.exc.DBAPIError as e:
            msg = str(e.orig) if e.orig is not None else str(e)
            if "statement timeout" in msg or "canceling statement" in msg:
                raise QueryTimeout(f"query exceeded {self.timeout_ms} ms") from e
            raise QueryError(msg.strip().splitlines()[0][:500]) from e
        except sqlite3.Error as e:
            raise QueryError(str(e)[:500]) from e
        elapsed = (time.perf_counter() - start) * 1000
        return QueryResult(columns=columns, rows=rows, truncated=truncated, elapsed_ms=elapsed)

    def _run_sqlite(self, sql: str, max_rows: int) -> tuple[list[str], list[list[Any]], bool]:
        with self.engine.connect() as conn:
            raw: sqlite3.Connection = conn.connection.driver_connection  # type: ignore[assignment]
            deadline = time.monotonic() + self.timeout_ms / 1000
            timed_out = False

            def progress() -> int:
                nonlocal timed_out
                if time.monotonic() > deadline:
                    timed_out = True
                    return 1  # non-zero aborts the statement
                return 0

            raw.set_progress_handler(progress, 10_000)
            # The authorizer is installed only around the model's statement: SQLAlchemy's own
            # connection setup runs PRAGMAs that it would (correctly) deny.
            raw.set_authorizer(_sqlite_authorizer)
            try:
                cur = raw.execute(sql)
                columns = [d[0] for d in cur.description or []]
                fetched = cur.fetchmany(max_rows + 1)
            except sqlite3.OperationalError as e:
                if timed_out or "interrupted" in str(e):
                    raise QueryTimeout(f"query exceeded {self.timeout_ms} ms") from e
                raise
            finally:
                raw.set_authorizer(None)
                raw.set_progress_handler(None, 0)
        truncated = len(fetched) > max_rows
        rows = [[_jsonable(v) for v in r] for r in fetched[:max_rows]]
        return columns, rows, truncated

    def _run_postgres(self, sql: str, max_rows: int) -> tuple[list[str], list[list[Any]], bool]:
        with self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN READ ONLY")
            try:
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}")
                # exec_driver_sql with no params: a literal `%` in the query isn't a placeholder.
                cur = conn.exec_driver_sql(sql.replace("%", "%%"))
                columns = list(cur.keys())
                fetched = cur.fetchmany(max_rows + 1)
            finally:
                conn.exec_driver_sql("ROLLBACK")
        truncated = len(fetched) > max_rows
        rows = [[_jsonable(v) for v in r] for r in fetched[:max_rows]]
        return columns, rows, truncated

    def ping(self) -> bool:
        try:
            self.run("SELECT 1", max_rows=1)
            return True
        except Exception:
            return False
