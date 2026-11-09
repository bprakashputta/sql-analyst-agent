import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from sql_analyst.llm import LLM, BudgetExceeded, ResponseCache, SpendLedger, price
from sql_analyst.promptlib import Prompt, load


class Out(BaseModel):
    value: int


PROMPT = Prompt("t", 1, "system {{x}}", "user {{y}}")


class FakeRunnable:
    def __init__(self):
        self.n = 0

    def invoke(self, messages):
        self.n += 1
        raw = SimpleNamespace(
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 100,
                "input_token_details": {"cache_read": 400},
            },
            response_metadata={"stop_reason": "end_turn"},
        )
        return {"raw": raw, "parsed": Out(value=self.n), "parsing_error": None}


def make_llm(tmp_path, cap=5.0, cache=True):
    llm = LLM(
        model="claude-haiku-5-5",
        effort="medium",
        ledger=SpendLedger(tmp_path / "spend.jsonl", cap),
        cache=ResponseCache(tmp_path / "c.sqlite") if cache else None,
        api_key="test",
    )
    fake = FakeRunnable()
    llm._runnable = lambda schema: fake  # type: ignore[method-assign]
    return llm, fake


def test_price_haiku_with_cache_read():
    usd = price(
        "claude-haiku-5-5",
        {"input_tokens": 1000, "output_tokens": 100, "input_token_details": {"cache_read": 400}},
    )
    assert usd == pytest.approx((600 * 0.10 + 400 * 0.01 + 100 * 0.50) / 1e6)


def test_call_is_logged_and_cached(tmp_path):
    llm, fake = make_llm(tmp_path)
    a, rec = llm.structured(PROMPT, Out, purpose="t", x=1, y=2)
    b, rec2 = llm.structured(PROMPT, Out, purpose="t", x=1, y=2)
    assert a.value == b.value == 1 and fake.n == 1
    assert rec2.cached and rec2.usd == 0
    lines = (tmp_path / "spend.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["input_tokens"] == 1000
    c, _ = llm.structured(PROMPT, Out, purpose="t", x=1, y=3)  # different request: a real call
    assert c.value == 2


def test_spend_cap_blocks_the_call(tmp_path):
    llm, fake = make_llm(tmp_path, cap=0.0001, cache=False)
    with pytest.raises(BudgetExceeded):
        llm.structured(PROMPT, Out, purpose="t", x=1, y=2)
    assert fake.n == 0


def test_ledger_total(tmp_path):
    llm, _ = make_llm(tmp_path, cache=False)
    llm.structured(PROMPT, Out, purpose="t", x=1, y=2)
    llm.structured(PROMPT, Out, purpose="t", x=1, y=2)
    assert llm.ledger.total() == pytest.approx(2 * (600 * 0.10 + 400 * 0.01 + 100 * 0.50) / 1e6)


def test_unknown_model_rejected(tmp_path):
    with pytest.raises(ValueError):
        LLM(
            model="gpt-x",
            effort="low",
            ledger=SpendLedger(tmp_path / "s", 1),
            cache=None,
            api_key="k",
        )


def test_prompt_files_load_and_render():
    p = load("generate_sql")
    assert p.version >= 1
    with pytest.raises(KeyError):
        p.render(dialect="sqlite")
    for name in ("explain", "review_result"):
        assert load(name).id.startswith(name + "@v")
