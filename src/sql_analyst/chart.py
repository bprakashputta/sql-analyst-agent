"""Pick a chart from the shape of a result, by rules. No model call.

| Result shape                                         | Chart                 |
|------------------------------------------------------|-----------------------|
| 1 row x 1 column                                     | number (a stat tile)  |
| a time-like column + 1-3 numeric columns, >= 3 rows  | line                  |
| 1 label column + 1 numeric column, 2-30 rows         | bar (sorted, horizontal) |
| 1 label column + 2-3 numeric columns, 2-15 rows      | grouped bar           |
| exactly 2 numeric columns, >= 8 rows                 | scatter               |
| anything else                                        | table only            |

A column is numeric if every non-null value is a number (bools excluded); time-like if its name says
so (date, month, year, ...) or every value parses as an ISO date or a year between 1900 and 2100.
Bars are sorted by value unless the labels look ordinal (numbers or dates), and the chart says why it
was chosen.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Literal

from pydantic import BaseModel

ChartType = Literal["number", "line", "bar", "grouped_bar", "scatter", "none"]

_TIME_NAME = re.compile(r"(^|_|\b)(date|day|week|month|year|quarter|period|time|ts)(_|\b|$)", re.I)


class ChartSpec(BaseModel):
    type: ChartType
    x: str | None = None
    y: list[str] = []
    reason: str
    sort: Literal["value_desc", "x_asc", "none"] = "none"


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _numeric(rows: list[list[Any]], i: int) -> bool:
    vals = [r[i] for r in rows if r[i] is not None]
    return bool(vals) and all(_is_number(v) for v in vals)


def _looks_time(name: str, rows: list[list[Any]], i: int) -> bool:
    vals = [r[i] for r in rows if r[i] is not None]
    if not vals:
        return False
    if _TIME_NAME.search(name):
        return True
    if all(isinstance(v, int) and 1900 <= v <= 2100 for v in vals) and len(set(vals)) == len(vals):
        return "year" in name.lower()
    if all(isinstance(v, str) for v in vals):
        try:
            for v in vals:
                dt.date.fromisoformat(v[:10])
            return True
        except ValueError:
            return False
    return False


def choose(columns: list[str], rows: list[list[Any]]) -> ChartSpec:
    n_rows, n_cols = len(rows), len(columns)
    if n_rows == 0:
        return ChartSpec(type="none", reason="no rows")
    if n_rows == 1 and n_cols == 1:
        if _is_number(rows[0][0]):
            return ChartSpec(type="number", y=[columns[0]], reason="a single value")
        return ChartSpec(type="none", reason="a single non-numeric value")

    numeric = [i for i in range(n_cols) if _numeric(rows, i)]
    other = [i for i in range(n_cols) if i not in numeric]

    time_cols = [i for i in range(n_cols) if _looks_time(columns[i], rows, i)]
    if time_cols and n_rows >= 3:
        t = time_cols[0]
        ys = [i for i in numeric if i != t]
        if 1 <= len(ys) <= 3 and n_cols == len(ys) + 1:
            return ChartSpec(
                type="line",
                x=columns[t],
                y=[columns[i] for i in ys],
                sort="x_asc",
                reason=f"{columns[t]} is a time axis with {len(ys)} measure(s)",
            )

    if len(other) == 1 and n_cols == len(numeric) + 1:
        label = columns[other[0]]
        labels = [r[other[0]] for r in rows]
        ordinal = all(isinstance(v, int | float) or v is None for v in labels)
        if len(numeric) == 1 and 2 <= n_rows <= 30:
            return ChartSpec(
                type="bar",
                x=label,
                y=[columns[numeric[0]]],
                sort="x_asc" if ordinal else "value_desc",
                reason=f"one measure across {n_rows} {label} values",
            )
        if 2 <= len(numeric) <= 3 and 2 <= n_rows <= 15:
            return ChartSpec(
                type="grouped_bar",
                x=label,
                y=[columns[i] for i in numeric],
                sort="none",
                reason=f"{len(numeric)} measures across {n_rows} {label} values",
            )

    if n_cols == 2 and len(numeric) == 2 and n_rows >= 8:
        return ChartSpec(
            type="scatter",
            x=columns[0],
            y=[columns[1]],
            reason=f"two measures over {n_rows} rows",
        )

    if n_rows > 30 and len(other) == 1 and len(numeric) == 1:
        return ChartSpec(
            type="none", reason=f"{n_rows} categories is too many to chart; see the table"
        )
    return ChartSpec(type="none", reason="no chart fits this result shape; see the table")
