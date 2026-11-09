from fastapi.testclient import TestClient

from sql_analyst.agent import Agent, AgentConfig, Deps
from sql_analyst.api import create_app
from sql_analyst.config import Settings
from sql_analyst.llm import SpendLedger

from .conftest import FakeLLM


class FakeState:
    def __init__(self, db, schema, script, tmp_path):
        self.settings = Settings(anthropic_api_key="test", data_dir=tmp_path, _env_file=None)
        self.db, self.schema = db, schema
        self.ledger = SpendLedger(tmp_path / "spend.jsonl", 5.0)
        self.llm = FakeLLM(script)
        self.agent = Agent(Deps(llm=self.llm, db=db, schema=schema, config=AgentConfig()))


def client(shop_db, shop_schema, tmp_path, script):
    st = FakeState(shop_db, shop_schema, script, tmp_path)
    app = create_app(st.settings, state=st)
    return TestClient(app)


def test_health_ready_schema(shop_db, shop_schema, tmp_path):
    with client(shop_db, shop_schema, tmp_path, {}) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/ready").json()["ready"] is True
        tables = {t["name"] for t in c.get("/api/schema").json()["tables"]}
        assert tables == {"customers", "orders", "order_items", "products"}
        assert c.get("/api/spend").json()["cap_usd"] == 5.0
        assert "SQL Analyst" in c.get("/").text


def test_ask(shop_db, shop_schema, tmp_path):
    script = {
        "SqlDraft": [
            {
                "needs_clarification": False,
                "sql": "SELECT country, COUNT(*) AS n FROM customers GROUP BY country",
            }
        ],
        "Explanation": [{"answer": "Brazil has 2 customers and Canada 1.", "caveats": []}],
    }
    with client(shop_db, shop_schema, tmp_path, script) as c:
        r = c.post("/api/ask", json={"question": "Customers per country?"})
        body = r.json()
        assert r.status_code == 200, body
        assert body["status"] == "answered"
        assert body["chart"]["type"] == "bar"
        assert body["row_count"] == 2 and body["columns"] == ["country", "n"]
        assert body["usage"]["llm_calls"] == 2
        assert r.headers["x-request-id"]


def test_ask_validation(shop_db, shop_schema, tmp_path):
    with client(shop_db, shop_schema, tmp_path, {}) as c:
        assert c.post("/api/ask", json={"question": ""}).status_code == 422


def test_spend_cap_is_402(shop_db, shop_schema, tmp_path):
    from sql_analyst.llm import BudgetExceeded

    with client(
        shop_db, shop_schema, tmp_path, {"SqlDraft": [BudgetExceeded("spend cap reached")]}
    ) as c:
        assert c.post("/api/ask", json={"question": "anything?"}).status_code == 402
