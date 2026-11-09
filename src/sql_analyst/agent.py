"""The analyst agent: a LangGraph state machine from question to checked answer.

    link_schema ──► generate_sql ──► guard ──► execute ──► chart ──► explain ──► END
                       ▲   │           │          │
                       │   └─► clarify ──► END    │  (asks the user; no SQL runs)
                       └──── retry ◄──────────────┘
                             (guard refusal, DB error, timeout, or empty / all-NULL result,
                              while attempts remain; escalates to the full schema when the
                              error names a missing table or column)

Every node appends to `trace` (what it did, how long it took), every model call to `calls`
(tokens, cost, latency), and every SQL attempt to `attempts`, so the UI and the eval can show
exactly what happened.
"""

from __future__ import annotations

import operator
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from . import chart as chart_rules
from . import promptlib
from .db import Database, QueryError, QueryTimeout
from .guard import check_sql
from .llm import LLM, BudgetExceeded, LLMOutputError
from .schema import Schema, link, render
from .verify import unverified_numbers

# -- model output schemas --------------------------------------------------------------------------


class SqlDraft(BaseModel):
    needs_clarification: bool = Field(
        description="True only if the question is ambiguous in a way that changes the answer"
    )
    clarifying_question: str | None = Field(None, description="One short question for the user")
    options: list[str] = Field(default_factory=list, description="2-4 concrete interpretations")
    assumptions: list[str] = Field(default_factory=list, description="Assumptions the query makes")
    sql: str | None = Field(
        None, description="One SELECT statement, or null if clarifying or declining"
    )
    cannot_answer: str | None = Field(
        None,
        description="Set (and leave sql null) only if no read-only SELECT can answer: the request "
        "asks to change data or settings, or the data isn't in the schema. Say why in one sentence.",
    )


class Review(BaseModel):
    verdict: Literal["ok", "revise"]
    problem: str | None = Field(None, description="The concrete problem, when verdict is revise")


class Explanation(BaseModel):
    answer: str
    caveats: list[str] = Field(default_factory=list)


# -- configuration ---------------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """Switches the eval compares. The defaults are the production configuration."""

    link_schema: bool = True
    sample_values: bool = True
    max_attempts: int = 3
    retry_on_empty: bool = True
    review_result: bool = False
    escalate_schema: bool = True
    clarify: bool = True
    explain: bool = True
    max_link_tables: int = 6
    # Link only when the full schema card is bigger than this (~4 chars per token). Small schemas are
    # sent whole: on BIRD's 3-13 table databases linking saved ~13% of prompt tokens and bought
    # nothing (ADR 003). 0 = always link.
    link_token_budget: int = 3000
    row_limit: int = 500
    max_rows: int = 500


@dataclass
class Deps:
    llm: LLM
    db: Database
    schema: Schema
    config: AgentConfig = field(default_factory=AgentConfig)


# -- state -----------------------------------------------------------------------------------------


class Attempt(TypedDict):
    sql: str
    status: Literal["ok", "refused", "error", "timeout", "empty", "revise"]
    error: str | None
    row_count: int | None


class State(TypedDict, total=False):
    question: str
    evidence: str
    clarification: str
    schema_tables: list[str]
    schema_scope: str
    full_schema: bool
    draft: dict[str, Any]
    sql: str
    executed_sql: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    status: Literal["answered", "clarify", "declined", "failed"]
    error: str
    chart: dict[str, Any]
    answer: str
    caveats: list[str]
    unverified: list[str]
    attempts: Annotated[list[Attempt], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
    calls: Annotated[list[dict[str, Any]], operator.add]


_MISSING_OBJECT = re.compile(r"no such (table|column)|does not exist|unknown table|undefined", re.I)

_DIALECT_NOTES = {
    "sqlite": (
        "SQLite: integer / integer is integer division, so CAST one side AS REAL for ratios and "
        "percentages; dates are TEXT, use strftime() / date(); there is no ILIKE (LIKE is "
        "case-insensitive for ASCII)."
    ),
    "postgres": (
        "Postgres: integer / integer truncates, so cast to numeric for ratios; use date_trunc() and "
        "EXTRACT() for dates; ILIKE for case-insensitive matching; ROUND(x::numeric, n)."
    ),
}

_CLARIFY_ON = (
    "If the question could reasonably mean two things that give different answers (for example "
    "'best customers' by revenue or by number of orders, or a time period that isn't stated) and "
    "neither the schema nor the hints settle it, set needs_clarification=true, ask one short "
    "question and give 2-4 options. Otherwise answer, and list any small assumptions you made."
)
_CLARIFY_OFF = (
    "Never ask for clarification. If the question is ambiguous, choose the most likely reading and "
    "state it in assumptions."
)


def _step(node: str, start: float, **detail: Any) -> dict[str, Any]:
    return {"node": node, "ms": round((time.perf_counter() - start) * 1000, 1), **detail}


def _feedback(attempts: list[Attempt]) -> str:
    if not attempts:
        return ""
    lines = ["", "Your previous attempts at this question failed. Fix the query."]
    for i, a in enumerate(attempts, 1):
        lines.append(f"Attempt {i}:\n{a['sql']}")
        if a["status"] == "revise":
            lines.append(f"Result: it ran, but a reviewer found a problem: {a['error']}")
        elif a["status"] == "empty":
            lines.append(
                "Result: it ran but returned no rows (or only NULLs). Check that text filters match the "
                "sample values exactly (spelling, case, codes vs labels), that joins use the right keys, "
                "and that dates are compared in the stored format. If you are sure the true answer is "
                "empty, return the same query."
            )
        else:
            lines.append(f"Result: {a['status']}: {a['error']}")
    return "\n".join(lines)


def build_graph(deps: Deps) -> Any:
    cfg = deps.config
    prompt_sql = promptlib.load("generate_sql")
    prompt_explain = promptlib.load("explain")
    prompt_review = promptlib.load("review_result")
    allowed = deps.schema.table_names

    def link_schema(state: State) -> State:
        t0 = time.perf_counter()
        all_tables = list(deps.schema.tables)
        full_tokens = len(render(deps.schema, all_tables, with_samples=cfg.sample_values)) // 4
        if cfg.link_schema and not state.get("full_schema") and full_tokens > cfg.link_token_budget:
            res = link(
                deps.schema,
                state["question"],
                state.get("evidence", ""),
                max_tables=cfg.max_link_tables,
            )
            tables = res.tables
        else:
            tables = all_tables
        scope = (
            f"all {len(all_tables)} tables"
            if len(tables) == len(all_tables)
            else f"{len(tables)} of {len(all_tables)} tables, picked for this question"
        )
        return {
            "schema_tables": tables,
            "schema_scope": scope,
            "trace": [_step("link_schema", t0, tables=tables, scope=scope)],
        }

    def generate_sql(state: State) -> State:
        t0 = time.perf_counter()
        attempts = state.get("attempts", [])
        question = state["question"]
        if state.get("clarification"):
            question += f"\n(The user clarified: {state['clarification']})"
        schema_text = render(deps.schema, state["schema_tables"], with_samples=cfg.sample_values)
        may_clarify = cfg.clarify and not attempts and not state.get("clarification")
        try:
            draft, rec = deps.llm.structured(
                prompt_sql,
                SqlDraft,
                purpose="generate_sql" if not attempts else "retry_sql",
                dialect=deps.db.dialect,
                dialect_notes=_DIALECT_NOTES.get(deps.db.dialect, ""),
                clarify_policy=_CLARIFY_ON if may_clarify else _CLARIFY_OFF,
                schema_scope=state["schema_scope"],
                schema=schema_text,
                question=question,
                evidence=state.get("evidence") or "none",
                feedback=_feedback(attempts),
            )
        except (LLMOutputError, BudgetExceeded) as e:
            return {
                "status": "failed",
                "error": str(e),
                "trace": [_step("generate_sql", t0, error=str(e))],
            }
        call = {**rec.__dict__}
        if may_clarify and draft.needs_clarification and draft.clarifying_question:
            return {
                "draft": draft.model_dump(),
                "status": "clarify",
                "calls": [call],
                "trace": [_step("generate_sql", t0, outcome="needs clarification")],
            }
        sql = (draft.sql or "").strip()
        if not sql and draft.cannot_answer:
            # The model declined (a write request, or data that isn't there). Retrying would only
            # pressure it into writing something; answer with the reason instead.
            return {
                "draft": draft.model_dump(),
                "status": "declined",
                "answer": draft.cannot_answer,
                "calls": [call],
                "trace": [
                    _step("generate_sql", t0, outcome="declined", reason=draft.cannot_answer)
                ],
            }
        return {
            "draft": draft.model_dump(),
            "sql": sql,
            "calls": [call],
            "trace": [
                _step("generate_sql", t0, attempt=len(attempts) + 1, sql=sql, cached=rec.cached)
            ],
        }

    def widen_schema(state: State) -> State:
        return {
            "status": "answered",
            "full_schema": True,
            "schema_tables": list(deps.schema.tables),
            "schema_scope": f"all {len(deps.schema.tables)} tables",
            "trace": [
                _step(
                    "widen_schema",
                    time.perf_counter(),
                    reason="clarification requested on a partial schema",
                )
            ],
        }

    def clarify(state: State) -> State:
        return {
            "trace": [
                _step(
                    "clarify", time.perf_counter(), question=state["draft"]["clarifying_question"]
                )
            ]
        }

    def guard(state: State) -> State:
        t0 = time.perf_counter()
        res = check_sql(
            state.get("sql", ""),
            dialect=deps.db.dialect,
            allowed_tables=allowed,
            row_limit=cfg.row_limit,
        )
        if not res.ok:
            att: Attempt = {
                "sql": state.get("sql", ""),
                "status": "refused",
                "error": res.reason,
                "row_count": None,
            }
            return {
                "attempts": [att],
                "executed_sql": "",
                "trace": [_step("guard", t0, ok=False, reason=res.reason)],
            }
        return {
            "executed_sql": res.sql or "",
            "trace": [
                _step(
                    "guard", t0, ok=True, limit_applied=res.limit_applied, tables=list(res.tables)
                )
            ],
        }

    def execute(state: State) -> State:
        t0 = time.perf_counter()
        sql = state["executed_sql"]
        try:
            result = deps.db.run(sql, max_rows=cfg.max_rows)
        except QueryTimeout as e:
            att: Attempt = {
                "sql": state["sql"],
                "status": "timeout",
                "error": str(e),
                "row_count": None,
            }
            return {
                "attempts": [att],
                "rows": [],
                "columns": [],
                "trace": [_step("execute", t0, error=str(e))],
            }
        except QueryError as e:
            att = {"sql": state["sql"], "status": "error", "error": str(e), "row_count": None}
            return {
                "attempts": [att],
                "rows": [],
                "columns": [],
                "trace": [_step("execute", t0, error=str(e))],
            }
        empty = result.row_count == 0 or all(v is None for r in result.rows for v in r)
        att = {
            "sql": state["sql"],
            "status": "empty" if empty else "ok",
            "error": None,
            "row_count": result.row_count,
        }
        return {
            "attempts": [att],
            "columns": result.columns,
            "rows": result.rows,
            "truncated": result.truncated,
            "trace": [
                _step(
                    "execute",
                    t0,
                    rows=result.row_count,
                    truncated=result.truncated,
                    db_ms=round(result.elapsed_ms, 1),
                )
            ],
        }

    def review(state: State) -> State:
        """A second look at a result that ran fine: does it answer the question that was asked?"""
        t0 = time.perf_counter()
        rows = state.get("rows", [])
        table = "\n".join(" | ".join("NULL" if v is None else str(v) for v in r) for r in rows[:15])
        try:
            rev, rec = deps.llm.structured(
                prompt_review,
                Review,
                purpose="review",
                schema_scope=state["schema_scope"],
                schema=render(deps.schema, state["schema_tables"], with_samples=cfg.sample_values),
                question=state["question"],
                evidence=state.get("evidence") or "none",
                sql=state["sql"],
                row_count=len(rows),
                columns=", ".join(state.get("columns", [])),
                rows=table,
            )
        except (LLMOutputError, BudgetExceeded) as e:
            return {"trace": [_step("review", t0, error=str(e))]}
        update: State = {
            "calls": [{**rec.__dict__}],
            "trace": [_step("review", t0, verdict=rev.verdict, problem=rev.problem)],
        }
        if rev.verdict == "revise" and rev.problem:
            update["attempts"] = [
                {
                    "sql": state["sql"],
                    "status": "revise",
                    "error": rev.problem,
                    "row_count": len(rows),
                }
            ]
        return update

    def retry(state: State) -> State:
        last = state["attempts"][-1]
        escalate = (
            cfg.escalate_schema
            and cfg.link_schema
            and not state.get("full_schema")
            and last["status"] in ("error", "refused", "empty")
            and (last["status"] == "empty" or _MISSING_OBJECT.search(last["error"] or ""))
        )
        update: State = {
            "trace": [
                _step(
                    "retry",
                    time.perf_counter(),
                    reason=last["status"],
                    escalate_schema=bool(escalate),
                )
            ]
        }
        if escalate:
            update["full_schema"] = True
            update["schema_tables"] = list(deps.schema.tables)
            update["schema_scope"] = f"all {len(deps.schema.tables)} tables"
        return update

    def chart(state: State) -> State:
        t0 = time.perf_counter()
        spec = chart_rules.choose(state.get("columns", []), state.get("rows", []))
        return {
            "chart": spec.model_dump(),
            "status": "answered",
            "trace": [_step("chart", t0, type=spec.type, reason=spec.reason)],
        }

    def explain(state: State) -> State:
        t0 = time.perf_counter()
        rows = state.get("rows", [])
        shown = rows[:30]
        table = "\n".join(" | ".join("NULL" if v is None else str(v) for v in r) for r in shown)
        if len(rows) > len(shown):
            table += f"\n... {len(rows) - len(shown)} more rows"
        feedback = ""
        calls: list[dict[str, Any]] = []
        exp_obj: Explanation | None = None
        bad: list[str] = []
        for _ in range(2):  # one rewrite if the answer cites numbers that aren't in the result
            try:
                exp_obj, rec = deps.llm.structured(
                    prompt_explain,
                    Explanation,
                    purpose="explain",
                    question=state["question"],
                    sql=state["executed_sql"],
                    row_count=len(rows),
                    truncated=" (truncated)" if state.get("truncated") else "",
                    columns=", ".join(state.get("columns", [])),
                    rows=table + feedback,
                )
            except (LLMOutputError, BudgetExceeded) as e:
                return {
                    "answer": "",
                    "caveats": [f"explanation unavailable: {e}"],
                    "trace": [_step("explain", t0, error=str(e))],
                    "calls": calls,
                }
            calls.append({**rec.__dict__})
            bad = unverified_numbers(
                exp_obj.answer,
                rows,
                row_count=len(rows),
                context=state["question"] + " " + state["executed_sql"],
            )
            if not bad:
                break
            feedback = (
                f"\n\nYour previous answer cited numbers not in the result: {', '.join(bad)}. "
                "Use only numbers from the result."
            )
        assert exp_obj is not None
        return {
            "answer": exp_obj.answer,
            "caveats": exp_obj.caveats,
            "unverified": bad,
            "calls": calls,
            "trace": [_step("explain", t0, numbers_checked=not bad, unverified=bad)],
        }

    # -- routing ---------------------------------------------------------------------------------

    def after_generate(state: State) -> str:
        if state.get("status") == "clarify":
            # The model may be confused because linking left out the table it needs. Show it the
            # whole schema once before bothering the user.
            if len(state["schema_tables"]) < len(deps.schema.tables) and cfg.escalate_schema:
                return "widen_schema"
            return "clarify"
        if state.get("status") in ("failed", "declined"):
            return END
        return "guard"

    def can_retry(state: State) -> bool:
        return len(state.get("attempts", [])) < cfg.max_attempts

    def after_guard(state: State) -> str:
        if state.get("executed_sql"):
            return "execute"
        return "retry" if can_retry(state) else "give_up"

    def after_execute(state: State) -> str:
        last = state["attempts"][-1]
        if last["status"] == "ok":
            reviewed = sum(1 for a in state["attempts"] if a["status"] == "revise")
            if cfg.review_result and can_retry(state) and reviewed == 0:
                return "review"
            return "chart"
        if last["status"] == "empty":
            prev = state["attempts"][:-1]
            repeated = any(_norm(p["sql"]) == _norm(last["sql"]) for p in prev)
            if cfg.retry_on_empty and can_retry(state) and not repeated:
                return "retry"
            return "chart"  # an empty answer may be the true answer
        return "retry" if can_retry(state) else "give_up"

    def give_up(state: State) -> State:
        last = state["attempts"][-1]
        return {
            "status": "failed",
            "error": f"{last['status']}: {last['error']}",
            "trace": [_step("give_up", time.perf_counter(), attempts=len(state["attempts"]))],
        }

    def after_chart(state: State) -> str:
        return "explain" if cfg.explain else END

    g = StateGraph(State)
    g.add_node("link_schema", link_schema)
    g.add_node("generate_sql", generate_sql)
    g.add_node("clarify", clarify)
    g.add_node("widen_schema", widen_schema)
    g.add_node("guard", guard)
    g.add_node("execute", execute)
    g.add_node("review", review)
    g.add_node("retry", retry)
    g.add_node("give_up", give_up)
    g.add_node("chart", chart)
    g.add_node("explain", explain)
    g.set_entry_point("link_schema")
    g.add_edge("link_schema", "generate_sql")
    g.add_conditional_edges(
        "generate_sql", after_generate, ["clarify", "widen_schema", "guard", END]
    )
    g.add_edge("widen_schema", "generate_sql")
    g.add_edge("clarify", END)
    g.add_conditional_edges("guard", after_guard, ["execute", "retry", "give_up"])
    g.add_conditional_edges("execute", after_execute, ["chart", "review", "retry", "give_up"])
    g.add_conditional_edges(
        "review",
        lambda s: "retry" if s["attempts"][-1]["status"] == "revise" else "chart",
        ["retry", "chart"],
    )
    g.add_edge("retry", "generate_sql")
    g.add_edge("give_up", END)
    g.add_conditional_edges("chart", after_chart, ["explain", END])
    g.add_edge("explain", END)
    return g.compile()


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()


class Agent:
    """Convenience wrapper: one compiled graph per (database, config)."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self.graph = build_graph(deps)

    def ask(self, question: str, *, evidence: str = "", clarification: str = "") -> State:
        init: State = {
            "question": question,
            "evidence": evidence,
            "attempts": [],
            "trace": [],
            "calls": [],
        }
        if clarification:
            init["clarification"] = clarification
        return self.graph.invoke(init, {"recursion_limit": 40})
