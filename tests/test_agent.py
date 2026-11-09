import pytest

from sql_analyst.agent import Agent, AgentConfig, Deps
from sql_analyst.llm import BudgetExceeded

from .conftest import FakeLLM


def draft(sql=None, **kw):
    return {"needs_clarification": False, "sql": sql, "assumptions": [], **kw}


def explanation(text="Brazil has 2 customers.", caveats=()):
    return {"answer": text, "caveats": list(caveats)}


def make(shop_db, shop_schema, script, **cfg):
    cfg.setdefault("link_token_budget", 0)  # the test schema is tiny: force linking unless asked
    llm = FakeLLM(script)
    return Agent(Deps(llm=llm, db=shop_db, schema=shop_schema, config=AgentConfig(**cfg))), llm


def test_happy_path(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [draft("SELECT COUNT(*) AS n FROM customers WHERE country = 'Brazil'")],
            "Explanation": [explanation()],
        },
    )
    s = agent.ask("How many customers are in Brazil?")
    assert s["status"] == "answered"
    assert s["rows"] == [[2]]
    assert s["executed_sql"].endswith("LIMIT 500")
    assert s["chart"]["type"] == "number"
    assert s["answer"] == "Brazil has 2 customers." and s["unverified"] == []
    assert [t["node"] for t in s["trace"]] == [
        "link_schema",
        "generate_sql",
        "guard",
        "execute",
        "chart",
        "explain",
    ]
    assert "customers" in llm.calls[0]["schema"]


def test_db_error_feeds_back_and_retries(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [draft("SELECT nme FROM customers"), draft("SELECT name FROM customers")],
            "Explanation": [explanation("There are 3 customers.")],
        },
    )
    s = agent.ask("List customer names")
    assert s["status"] == "answered" and len(s["rows"]) == 3
    assert [a["status"] for a in s["attempts"]] == ["error", "ok"]
    assert "no such column" in llm.calls[1]["feedback"]
    assert llm.calls[1]["purpose"] == "retry_sql"


def test_guard_refusal_is_retried_and_never_executed(shop_db, shop_schema):
    agent, _ = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [draft("DELETE FROM customers"), draft("SELECT COUNT(*) FROM customers")],
            "Explanation": [explanation("There are 3 customers.")],
        },
    )
    s = agent.ask("Remove all customers")
    assert s["attempts"][0]["status"] == "refused"
    assert shop_db.run("SELECT COUNT(*) FROM customers", max_rows=1).rows == [[3]]


def test_gives_up_after_max_attempts(shop_db, shop_schema):
    agent, _ = make(
        shop_db, shop_schema, {"SqlDraft": [draft("DROP TABLE orders")] * 3}, max_attempts=3
    )
    s = agent.ask("drop orders")
    assert s["status"] == "failed"
    assert len(s["attempts"]) == 3
    assert "refused" in s["error"]


def test_empty_result_retries_once_then_escalates_schema(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [
                draft("SELECT name FROM customers WHERE country = 'brazil'"),
                draft("SELECT name FROM customers WHERE country = 'Brazil'"),
            ],
            "Explanation": [explanation("Ana and Chen.")],
        },
    )
    s = agent.ask("Which customers are in brazil?")
    assert [a["status"] for a in s["attempts"]] == ["empty", "ok"]
    retry = next(t for t in s["trace"] if t["node"] == "retry")
    assert retry["escalate_schema"] is True
    assert "all 4 tables" in llm.calls[1]["schema_scope"]


def test_same_empty_query_twice_is_accepted(shop_db, shop_schema):
    q = "SELECT name FROM customers WHERE country = 'Peru'"
    agent, _ = make(
        shop_db,
        shop_schema,
        {"SqlDraft": [draft(q), draft(q)], "Explanation": [explanation("No customers in Peru.")]},
    )
    s = agent.ask("Customers in Peru?")
    assert s["status"] == "answered" and s["rows"] == []
    assert len(s["attempts"]) == 2


def test_clarification_stops_before_sql(shop_db, shop_schema):
    agent, _ = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [
                draft(
                    None,
                    needs_clarification=True,
                    clarifying_question="Best by revenue or by orders?",
                    options=["By revenue", "By number of orders"],
                )
            ]
        },
        link_schema=False,
    )
    s = agent.ask("Who are our best customers?")
    assert s["status"] == "clarify"
    assert "executed_sql" not in s and s["attempts"] == []


def test_clarified_question_is_answered(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [draft("SELECT customer_id, SUM(total) AS revenue FROM orders GROUP BY 1")],
            "Explanation": [explanation("Customer 1 leads with 192.5.")],
        },
    )
    s = agent.ask("Who are our best customers?", clarification="By revenue")
    assert s["status"] == "answered"
    assert "By revenue" in llm.calls[0]["question"]
    assert "Never ask for clarification" in llm.calls[0]["clarify_policy"]


def test_invented_numbers_get_one_rewrite_then_flag(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [draft("SELECT COUNT(*) FROM customers")],
            "Explanation": [
                explanation("There are 7 customers."),
                explanation("There are 9 customers."),
            ],
        },
    )
    s = agent.ask("How many customers?")
    assert s["unverified"] == ["9"]
    assert "not in the result" in llm.calls[-1]["rows"]


def test_review_can_send_a_query_back(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [draft("SELECT id FROM customers"), draft("SELECT name FROM customers")],
            "Review": [
                {"verdict": "revise", "problem": "returns ids, the question asks for names"}
            ],
            "Explanation": [explanation("Ana, Ben and Chen.")],
        },
        review_result=True,
    )
    s = agent.ask("Customer names?")
    assert [a["status"] for a in s["attempts"]] == ["ok", "revise", "ok"]
    assert s["columns"] == ["name"]


def test_budget_exceeded_fails_cleanly(shop_db, shop_schema):
    agent, _ = make(shop_db, shop_schema, {"SqlDraft": [BudgetExceeded("spend cap reached: $5")]})
    s = agent.ask("How many customers?")
    assert s["status"] == "failed" and s["error"].startswith("spend cap")


def test_no_explain_mode(shop_db, shop_schema):
    agent, llm = make(shop_db, shop_schema, {"SqlDraft": [draft("SELECT 1")]}, explain=False)
    s = agent.ask("one?")
    assert s["status"] == "answered" and "answer" not in s
    assert len(llm.calls) == 1


@pytest.mark.parametrize("link", [True, False])
def test_link_switch(shop_db, shop_schema, link):
    agent, llm = make(
        shop_db, shop_schema, {"SqlDraft": [draft("SELECT 1")]}, explain=False, link_schema=link
    )
    agent.ask("What is the average order total?")
    assert ("TABLE products" in llm.calls[0]["schema"]) is (not link)


def test_small_schema_is_sent_whole(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {"SqlDraft": [draft("SELECT 1")]},
        explain=False,
        link_token_budget=3000,
    )
    agent.ask("What is the average order total?")
    assert llm.calls[0]["schema_scope"] == "all 4 tables"


def test_clarify_on_partial_schema_widens_first(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [
                draft(None, needs_clarification=True, clarifying_question="Which table?"),
                draft("SELECT SUM(total) FROM orders"),
            ],
        },
        explain=False,
    )
    s = agent.ask("What is the average order total?")
    assert s["status"] == "answered"
    assert "widen_schema" in [t["node"] for t in s["trace"]]
    assert llm.calls[1]["schema_scope"] == "all 4 tables"


def test_write_request_is_declined_without_retries(shop_db, shop_schema):
    agent, llm = make(
        shop_db,
        shop_schema,
        {
            "SqlDraft": [
                draft(None, cannot_answer="This assistant is read-only; it can't delete data.")
            ]
        },
    )
    s = agent.ask("Delete all customers from Brazil")
    assert s["status"] == "declined"
    assert "read-only" in s["answer"]
    assert s["attempts"] == [] and len(llm.calls) == 1
