from __future__ import annotations

import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from sql_analyst.db import Database
from sql_analyst.llm import CallRecord
from sql_analyst.schema import Schema, introspect


def build_shop(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, country TEXT);
        CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT, category TEXT, price REAL);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
                             order_date TEXT, total REAL);
        CREATE TABLE order_items (order_id INTEGER REFERENCES orders(id),
                                  product_id INTEGER REFERENCES products(id), qty INTEGER);
        INSERT INTO customers VALUES (1,'Ana','Brazil'),(2,'Ben','Canada'),(3,'Chen','Brazil');
        INSERT INTO products VALUES (1,'Lamp','Home',20.0),(2,'Desk','Office',150.0),(3,'Pen','Office',2.5);
        INSERT INTO orders VALUES (1,1,'2024-01-05',40.0),(2,2,'2024-02-11',150.0),(3,3,'2024-02-20',5.0),
                                  (4,1,'2024-03-02',152.5);
        INSERT INTO order_items VALUES (1,1,2),(2,2,1),(3,3,2),(4,2,1),(4,3,1);
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def shop_path(tmp_path: Path) -> Path:
    return build_shop(tmp_path / "shop.sqlite")


@pytest.fixture
def shop_db(shop_path: Path) -> Database:
    return Database.sqlite(shop_path, timeout_ms=1000)


@pytest.fixture
def shop_schema(shop_db: Database) -> Schema:
    return introspect(shop_db.meta_engine, "sqlite")


class FakeLLM:
    """Stands in for sql_analyst.llm.LLM: returns scripted answers per output type, no network."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {k: deque(v) for k, v in script.items()}
        self.calls: list[dict[str, Any]] = []

    def structured(self, prompt: Any, output: type, /, *, purpose: str, **variables: Any):
        self.calls.append({"purpose": purpose, "output": output.__name__, **variables})
        item = self.script[output.__name__].popleft()
        if isinstance(item, Exception):
            raise item
        obj = item if isinstance(item, output) else output.model_validate(item)
        rec = CallRecord(time.time(), purpose, "fake", prompt.id, 100, 20, 0, 0, 0.0001, 1.0)
        return obj, rec
