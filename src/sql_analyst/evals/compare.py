"""Execution accuracy: does the predicted query return the same result as the gold query?

`ex_set` is BIRD's official comparison: the two results are equal as *sets* of row tuples (row order
and duplicates ignored, column order significant). `ex_ordered` is stricter: when the gold query's
outermost statement has an ORDER BY, rows must also come back in the same order (as lists);
otherwise duplicates must match (as multisets).

Floats are rounded to 6 decimal places before comparing, so `100.0 * a / b` and `a * 100.0 / b` agree;
BIRD's own scorer compares raw values. Both are reported (`ex_set` and `ex_set_raw_floats` in
evals/results.json) so the effect is visible; the README headline uses the rounded comparison.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import sqlglot
from sqlglot import exp


def _norm_value(v: Any, ndigits: int | None) -> Any:
    if isinstance(v, float) and ndigits is not None:
        r = round(v, ndigits)
        return int(r) if r.is_integer() else r
    if isinstance(v, bool):
        return int(v)
    return v


def _norm_rows(rows: list[Any], ndigits: int | None) -> list[tuple[Any, ...]]:
    return [tuple(_norm_value(v, ndigits) for v in r) for r in rows]


def gold_is_ordered(gold_sql: str, dialect: str = "sqlite") -> bool:
    try:
        tree = sqlglot.parse_one(gold_sql, read=dialect)
    except Exception:
        return False
    return isinstance(tree, exp.Query) and tree.args.get("order") is not None


def ex_set(pred: list[Any], gold: list[Any], ndigits: int | None = 6) -> bool:
    return set(_norm_rows(pred, ndigits)) == set(_norm_rows(gold, ndigits))


def ex_ordered(pred: list[Any], gold: list[Any], ordered: bool, ndigits: int | None = 6) -> bool:
    p, g = _norm_rows(pred, ndigits), _norm_rows(gold, ndigits)
    if ordered:
        return p == g
    return Counter(p) == Counter(g)
