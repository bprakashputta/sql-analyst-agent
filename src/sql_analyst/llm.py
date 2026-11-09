"""Claude access: structured output, prices, a hard spend cap, a response cache and per-call logging.

Every model call goes through `LLM.structured`:

1. Build the messages from a versioned prompt file (prompts/*.md) and a cache key from
   (model, effort, prompt name + version, messages, output schema).
2. A cache hit returns the stored, already-validated answer for $0.
3. Otherwise check the spend ledger: if the total so far plus this call's worst case would cross the
   cap, raise `BudgetExceeded` instead of calling.
4. Call Claude through LangChain's ChatAnthropic with native structured output (`output_config.format`,
   JSON schema from the pydantic model), then validate the answer with pydantic.
5. Price the call from its token usage, append it to the ledger and log tokens, cost and latency.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from .promptlib import Prompt

log = structlog.get_logger()

# USD per 1M tokens (input, output). platform.claude.com pricing, via the claude-api skill (2026-10-06).
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_READ_MULT = {"claude-opus-5-5": 0.05}  # others: 0.10 of the input price
CACHE_WRITE_MULT = 1.25


class BudgetExceeded(RuntimeError):
    pass


class LLMOutputError(RuntimeError):
    pass


@dataclass
class CallRecord:
    ts: float
    purpose: str
    model: str
    prompt: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    usd: float
    latency_ms: float
    cached: bool = False


def price(model: str, usage: dict[str, Any]) -> float:
    pin, pout = PRICES[model]
    details = usage.get("input_token_details") or {}
    total_in = int(usage.get("input_tokens") or 0)
    read = int(details.get("cache_read") or 0)
    write = int(details.get("cache_creation") or 0)
    plain = max(total_in - read - write, 0)
    out = int(usage.get("output_tokens") or 0)
    usd = plain * pin + read * pin * CACHE_READ_MULT.get(model, 0.10)
    usd += write * pin * CACHE_WRITE_MULT + out * pout
    return usd / 1e6


class SpendLedger:
    """Append-only JSONL of every billed call. Shared by the API, the CLI and the eval runner."""

    def __init__(self, path: Path, cap_usd: float) -> None:
        self.path = path
        self.cap_usd = cap_usd
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def total(self) -> float:
        if not self.path.exists():
            return 0.0
        with self.path.open() as f:
            return sum(json.loads(line)["usd"] for line in f if line.strip())

    def check(self, worst_case_usd: float) -> None:
        spent = self.total()
        if spent + worst_case_usd > self.cap_usd:
            raise BudgetExceeded(
                f"spend cap reached: ${spent:.4f} spent, cap ${self.cap_usd:.2f}, "
                f"next call could cost up to ${worst_case_usd:.4f}"
            )

    def record(self, rec: CallRecord) -> None:
        line = json.dumps(asdict(rec)) + "\n"
        with self._lock, self.path.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            fcntl.flock(f, fcntl.LOCK_UN)


class ResponseCache:
    """SQLite cache of validated structured answers keyed by the exact request."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, ts REAL)"
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time()),
            )


class LLM:
    def __init__(
        self,
        *,
        model: str,
        effort: str,
        ledger: SpendLedger,
        cache: ResponseCache | None,
        api_key: str | None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        max_tokens: int = 4000,
    ) -> None:
        if model not in PRICES:
            raise ValueError(f"no price for model {model!r}; add it to PRICES")
        self.model = model
        self.effort = effort
        self.ledger = ledger
        self.cache = cache
        self.max_tokens = max_tokens
        self._chat = ChatAnthropic(
            model=model,
            api_key=api_key,
            max_tokens=max_tokens,
            timeout=timeout_s,
            max_retries=max_retries,
            effort=effort,
        )
        self._runnables: dict[type[BaseModel], Any] = {}

    def _runnable(self, schema: type[BaseModel]) -> Any:
        if schema not in self._runnables:
            self._runnables[schema] = self._chat.with_structured_output(
                schema, method="json_schema", include_raw=True
            )
        return self._runnables[schema]

    def structured[T: BaseModel](
        self, prompt: Prompt, output: type[T], /, *, purpose: str, **variables: Any
    ) -> tuple[T, CallRecord]:
        system, user = prompt.render(**variables)
        key = hashlib.sha256(
            json.dumps(
                [
                    self.model,
                    self.effort,
                    prompt.name,
                    prompt.version,
                    system,
                    user,
                    output.model_json_schema(),
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()

        if self.cache is not None and (hit := self.cache.get(key)) is not None:
            rec = CallRecord(
                time.time(),
                purpose,
                self.model,
                prompt.id,
                hit["input_tokens"],
                hit["output_tokens"],
                0,
                0,
                0.0,
                0.0,
                cached=True,
            )
            log.info("llm.call", **asdict(rec))
            return output.model_validate(hit["parsed"]), rec

        pin, pout = PRICES[self.model]
        worst = (
            (len(system) + len(user)) / 3 * pin * CACHE_WRITE_MULT + self.max_tokens * pout
        ) / 1e6
        self.ledger.check(worst)

        messages = [
            # The system block (instructions + schema card) is the stable prefix: mark it for prompt
            # caching so self-correction retries on the same question read it at 10% of the price.
            SystemMessage(
                content=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            ),
            HumanMessage(content=user),
        ]
        start = time.perf_counter()
        out = self._runnable(output).invoke(messages)
        latency = (time.perf_counter() - start) * 1000
        raw = out["raw"]
        usage = dict(getattr(raw, "usage_metadata", None) or {})
        details = usage.get("input_token_details") or {}
        rec = CallRecord(
            ts=time.time(),
            purpose=purpose,
            model=self.model,
            prompt=prompt.id,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read=int(details.get("cache_read") or 0),
            cache_write=int(details.get("cache_creation") or 0),
            usd=price(self.model, usage),
            latency_ms=latency,
        )
        self.ledger.record(rec)
        log.info("llm.call", **asdict(rec))

        parsed = out.get("parsed")
        if out.get("parsing_error") or parsed is None:
            stop = (getattr(raw, "response_metadata", None) or {}).get("stop_reason")
            raise LLMOutputError(f"model output did not match {output.__name__} (stop={stop})")
        try:
            obj = parsed if isinstance(parsed, output) else output.model_validate(parsed)
        except ValidationError as e:
            raise LLMOutputError(str(e)) from e

        if self.cache is not None:
            self.cache.put(
                key,
                {
                    "parsed": obj.model_dump(),
                    "input_tokens": rec.input_tokens,
                    "output_tokens": rec.output_tokens,
                },
            )
        return obj, rec
