"""FastAPI service: the analyst agent over one configured database, plus a small UI.

GET  /health       liveness: the process is up
GET  /ready        readiness: the database answers and an API key is configured
GET  /api/schema   tables, columns and row counts the agent can see
POST /api/ask      {question, clarification?} -> answer, SQL, rows, chart, trace, cost
GET  /api/spend    total spend so far and the cap
GET  /             the UI (static/index.html)
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .agent import Agent, AgentConfig, Deps
from .config import Settings, get_settings
from .db import Database
from .llm import LLM, ResponseCache, SpendLedger
from .schema import introspect

STATIC = Path(__file__).parent / "static"
log = structlog.get_logger()


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    clarification: str | None = Field(None, max_length=500)


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database.from_url(settings.database_url, timeout_ms=settings.statement_timeout_ms)
        self.schema = introspect(self.db.meta_engine, self.db.dialect)
        self.ledger = SpendLedger(settings.ledger_path, settings.spend_cap_usd)
        self.llm = LLM(
            model=settings.model,
            effort=settings.effort,
            ledger=self.ledger,
            cache=ResponseCache(settings.cache_path) if settings.llm_cache else None,
            api_key=settings.anthropic_api_key,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
        cfg = AgentConfig(
            max_attempts=settings.max_attempts,
            clarify=settings.clarify,
            max_link_tables=settings.max_link_tables,
            row_limit=settings.row_limit,
            max_rows=settings.row_limit,
        )
        self.agent = Agent(Deps(llm=self.llm, db=self.db, schema=self.schema, config=cfg))


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        app.state.s = state or AppState(settings)
        log.info(
            "startup",
            dialect=app.state.s.db.dialect,
            tables=len(app.state.s.schema.tables),
            model=settings.model,
        )
        yield

    app = FastAPI(title="sql-analyst-agent", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(request_id=rid)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["x-request-id"] = rid
        log.info(
            "http",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round((time.perf_counter() - t0) * 1000, 1),
            request_id=rid,
        )
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request) -> JSONResponse:
        s: AppState = request.app.state.s
        checks = {"database": s.db.ping(), "api_key": bool(s.settings.anthropic_api_key)}
        ok = all(checks.values())
        return JSONResponse({"ready": ok, **checks}, status_code=200 if ok else 503)

    @app.get("/api/schema")
    def schema(request: Request) -> dict[str, Any]:
        s: AppState = request.app.state.s
        return {
            "dialect": s.db.dialect,
            "tables": [
                {"name": t.name, "rows": t.row_count, "columns": [c.name for c in t.columns]}
                for t in s.schema.tables.values()
            ],
        }

    @app.get("/api/spend")
    def spend(request: Request) -> dict[str, float]:
        s: AppState = request.app.state.s
        return {"spent_usd": round(s.ledger.total(), 6), "cap_usd": s.ledger.cap_usd}

    @app.post("/api/ask")
    async def ask(body: AskRequest, request: Request) -> dict[str, Any]:
        s: AppState = request.app.state.s
        t0 = time.perf_counter()
        state = await run_in_threadpool(
            s.agent.ask, body.question, clarification=body.clarification or ""
        )
        if state.get("status") == "failed" and (state.get("error") or "").startswith("spend cap"):
            raise HTTPException(status_code=402, detail=state["error"])
        calls = state.get("calls", [])
        draft = state.get("draft") or {}
        return {
            "status": state.get("status"),
            "question": body.question,
            "answer": state.get("answer"),
            "caveats": state.get("caveats", []),
            "assumptions": draft.get("assumptions", []),
            "unverified_numbers": state.get("unverified", []),
            "clarifying_question": draft.get("clarifying_question")
            if state.get("status") == "clarify"
            else None,
            "options": draft.get("options", []) if state.get("status") == "clarify" else [],
            "sql": state.get("executed_sql") or None,
            "columns": state.get("columns", []),
            "rows": state.get("rows", []),
            "row_count": len(state.get("rows", [])),
            "truncated": state.get("truncated", False),
            "chart": state.get("chart"),
            "error": state.get("error"),
            "attempts": state.get("attempts", []),
            "trace": state.get("trace", []),
            "schema_scope": state.get("schema_scope"),
            "usage": {
                "llm_calls": len(calls),
                "input_tokens": sum(c["input_tokens"] for c in calls),
                "output_tokens": sum(c["output_tokens"] for c in calls),
                "usd": round(sum(c["usd"] for c in calls), 6),
                "cached_calls": sum(1 for c in calls if c.get("cached")),
                "model": s.settings.model,
            },
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main() -> None:
    import uvicorn

    uvicorn.run("sql_analyst.api:create_app", factory=True, host="0.0.0.0", port=8000)
