"""Run the agent over a BIRD split and score execution accuracy.

    python -m sql_analyst.evals run --config agent --split test
    python -m sql_analyst.evals report

Each question's outcome is appended to `evals/runs/<config>__<split>.jsonl` as soon as it finishes, so
an interrupted run resumes where it stopped. The spend ledger is shared with every other call this
project makes, and the run stops cleanly when the $ cap would be crossed.
"""

from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from ..agent import Agent, AgentConfig, Deps
from ..config import get_settings
from ..db import Database, QueryError, QueryTimeout
from ..guard import check_sql
from ..llm import LLM, BudgetExceeded, ResponseCache, SpendLedger
from ..schema import Schema, introspect
from .bird import SPLIT_SEED, Question, split
from .compare import ex_ordered, ex_set, gold_is_ordered

RUNS = Path("evals") / "runs"
EVAL_ROW_LIMIT = 100_000  # the eval must see whole results; the API default is 500
EVAL_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class EvalConfig:
    name: str
    description: str
    agent: AgentConfig
    model: str = "claude-haiku-5-5"
    effort: str = "medium"


def _agent_cfg(**kw: Any) -> AgentConfig:
    base = dict(clarify=False, explain=False, row_limit=EVAL_ROW_LIMIT, max_rows=EVAL_ROW_LIMIT)
    base.update(kw)
    return AgentConfig(**base)


_FULL = dict(link_schema=False, sample_values=True, max_attempts=3)
_BASE = dict(
    link_schema=False,
    sample_values=False,
    max_attempts=1,
    retry_on_empty=False,
    escalate_schema=False,
)
_AGENT = dict(link_schema=True, sample_values=True, max_attempts=3)

CONFIGS: dict[str, EvalConfig] = {
    c.name: c
    for c in [
        # Reported on the test split.
        EvalConfig(
            "baseline",
            "One shot, full schema (tables, types, keys), no sample values, no retries",
            _agent_cfg(**_BASE),
        ),
        EvalConfig(
            "full_schema_selfcorrect",
            "Full schema + sample values + self-correction (up to 3 attempts)",
            _agent_cfg(**_FULL),
        ),
        EvalConfig(
            "agent",
            "Schema linking + sample values + self-correction with schema escalation (production)",
            _agent_cfg(**_AGENT),
        ),
        EvalConfig(
            "agent_forced_linking",
            "The agent with schema linking forced on every question (token budget 0)",
            _agent_cfg(**_AGENT, link_token_budget=0),
        ),
        EvalConfig(
            "agent_sonnet",
            "The production agent on Claude Sonnet 5.5 instead of Haiku 5.5",
            _agent_cfg(**_AGENT),
            model="claude-sonnet-5-5",
        ),
        # Dev-only experiments that shaped the defaults (see ADR 005).
        EvalConfig("baseline_low", "baseline at effort=low", _agent_cfg(**_BASE), effort="low"),
        EvalConfig(
            "full_schema_selfcorrect_low",
            "full_schema_selfcorrect at effort=low",
            _agent_cfg(**_FULL),
            effort="low",
        ),
        EvalConfig(
            "agent_low",
            "agent (linking forced) at effort=low",
            _agent_cfg(**_AGENT, link_token_budget=0),
            effort="low",
        ),
        EvalConfig(
            "agent_review_low",
            "agent (linking forced) + a model review of each result, low",
            _agent_cfg(**_AGENT, link_token_budget=0, review_result=True),
            effort="low",
        ),
        EvalConfig(
            "agent_review",
            "agent (linking forced) + a model review of each result (one extra call)",
            _agent_cfg(**_AGENT, link_token_budget=0, review_result=True),
        ),
    ]
}


class _Resources:
    """One Database + Schema per BIRD database, shared by worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dbs: dict[str, tuple[Database, Schema]] = {}
        self._gold: dict[int, tuple[list[Any], str | None]] = {}

    def get(self, q: Question) -> tuple[Database, Schema]:
        with self._lock:
            if q.db_id not in self._dbs:
                db = Database.sqlite(q.db_path, timeout_ms=EVAL_TIMEOUT_MS)
                self._dbs[q.db_id] = (db, introspect(db.meta_engine, "sqlite"))
            return self._dbs[q.db_id]

    def gold(self, q: Question, db: Database) -> list[Any]:
        if q.question_id not in self._gold:
            res = db.run(q.gold_sql, max_rows=10_000_000)
            self._gold[q.question_id] = (res.rows, None)
        return self._gold[q.question_id][0]


def _tables(sql: str) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return set()
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return {t.name.lower() for t in tree.find_all(exp.Table) if t.name} - ctes


def _score(
    db: Database, sql: str | None, gold: list[Any], ordered: bool, allowed: frozenset[str]
) -> dict[str, Any]:
    if not sql:
        return {"ex_set": False, "ex_ordered": False, "ex_set_raw": False, "exec_error": "no sql"}
    g = check_sql(sql, dialect="sqlite", allowed_tables=allowed, row_limit=EVAL_ROW_LIMIT)
    if not g.ok:
        return {"ex_set": False, "ex_ordered": False, "ex_set_raw": False, "exec_error": g.reason}
    try:
        rows = db.run(g.sql or "", max_rows=EVAL_ROW_LIMIT).rows
    except (QueryError, QueryTimeout) as e:
        return {"ex_set": False, "ex_ordered": False, "ex_set_raw": False, "exec_error": str(e)}
    return {
        "ex_set": ex_set(rows, gold),
        "ex_ordered": ex_ordered(rows, gold, ordered),
        "ex_set_raw": ex_set(rows, gold, ndigits=None),
        "exec_error": None,
    }


def run_one(agent: Agent, q: Question, res: _Resources) -> dict[str, Any]:
    db, schema = res.get(q)
    gold = res.gold(q, db)
    ordered = gold_is_ordered(q.gold_sql)
    t0 = time.perf_counter()
    state = agent.ask(q.question, evidence=q.evidence)
    wall = time.perf_counter() - t0
    if (state.get("error") or "").startswith("spend cap"):
        raise BudgetExceeded(state["error"])
    attempts = state.get("attempts", [])
    final_sql = state.get("executed_sql") or (attempts[-1]["sql"] if attempts else None)
    first_sql = attempts[0]["sql"] if attempts else None
    final = _score(db, final_sql, gold, ordered, schema.table_names)
    first = _score(db, first_sql, gold, ordered, schema.table_names) if len(attempts) > 1 else final
    calls = state.get("calls", [])
    link_trace = next((t for t in state.get("trace", []) if t["node"] == "link_schema"), {})
    if str(link_trace.get("scope", "")).startswith("all "):
        link_trace = {}  # the whole schema was sent: recall is not a linking measurement
    gold_tables = _tables(q.gold_sql)
    linked = {t.lower() for t in link_trace.get("tables", [])}
    return {
        "question_id": q.question_id,
        "db_id": q.db_id,
        "difficulty": q.difficulty,
        "status": state.get("status"),
        "error": state.get("error"),
        "final_sql": final_sql,
        "first_sql": first_sql,
        "attempts": [{"status": a["status"], "error": a["error"]} for a in attempts],
        "n_attempts": len(attempts),
        "reviews": [t.get("verdict") for t in state.get("trace", []) if t["node"] == "review"],
        "escalated": any(t.get("escalate_schema") for t in state.get("trace", [])),
        **final,
        "first_ex_set": first["ex_set"],
        "linked_tables": sorted(linked),
        "link_recall": gold_tables <= linked if linked else None,
        "ordered_gold": ordered,
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "cache_read": sum(c["cache_read"] for c in calls),
        "usd": sum(c["usd"] for c in calls),
        "llm_calls": len(calls),
        "cached_calls": sum(1 for c in calls if c.get("cached")),
        "wall_s": round(wall, 2),
    }


def run(config_name: str, split_name: str, *, limit: int | None = None, workers: int = 6) -> Path:
    cfg = CONFIGS[config_name]
    settings = get_settings()
    questions = split(split_name)
    if limit:  # a fixed random subset (questions are grouped by database in file order)
        questions = sorted(
            random.Random(SPLIT_SEED).sample(questions, limit), key=lambda q: q.question_id
        )
    out = RUNS / f"{cfg.name}__{split_name}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = {json.loads(line)["question_id"] for line in out.read_text().splitlines() if line}
    todo = [q for q in questions if q.question_id not in done]
    print(
        f"{cfg.name} on {split_name}: {len(questions)} questions, {len(done)} done, {len(todo)} to run"
    )

    ledger = SpendLedger(settings.ledger_path, settings.spend_cap_usd)
    llm = LLM(
        model=cfg.model,
        effort=cfg.effort,
        ledger=ledger,
        cache=ResponseCache(settings.cache_path) if settings.llm_cache else None,
        api_key=settings.anthropic_api_key,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )
    res = _Resources()
    agents: dict[str, Agent] = {}
    agents_lock = threading.Lock()
    write_lock = threading.Lock()

    def agent_for(q: Question) -> Agent:
        db, schema = res.get(q)
        with agents_lock:
            if q.db_id not in agents:
                agents[q.db_id] = Agent(Deps(llm=llm, db=db, schema=schema, config=cfg.agent))
            return agents[q.db_id]

    for q in {q.db_id: q for q in todo}.values():  # introspect up front, single-threaded
        res.get(q)

    stop = threading.Event()

    def task(q: Question) -> dict[str, Any] | None:
        if stop.is_set():
            return None
        try:
            return run_one(agent_for(q), q, res)
        except BudgetExceeded as e:
            stop.set()
            print(f"stopping: {e}")
            return None

    n = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, q) for q in todo]
        for fut in as_completed(futures):
            row = fut.result()
            if row is None:
                continue
            n += 1
            with write_lock, out.open("a") as f:
                f.write(json.dumps(row) + "\n")
            if n % 20 == 0:
                print(f"  {n}/{len(todo)}  spent so far ${ledger.total():.4f}")
    print(f"wrote {out} (+{n}); ledger total ${ledger.total():.4f}")
    return out
