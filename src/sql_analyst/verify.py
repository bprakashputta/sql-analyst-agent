"""Check the numbers in a written answer against the result rows it claims to describe.

The explainer may round ("about 1.2 million") but may not invent. Every number in the answer must
match a cell (at the precision the answer wrote it, as a percentage of a fraction, or in thousands /
millions), the row count, or a number that already appears in the question.
"""

from __future__ import annotations

import math
import re
from typing import Any

_NUM = re.compile(
    r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?\s*(?:%|percent|million|billion|thousand|[kmb]\b)?", re.I
)


def _parse(token: str) -> tuple[float, int, str] | None:
    t = token.strip().lower().replace("$", "").replace(",", "")
    unit = ""
    for u, name in (
        ("%", "%"),
        ("percent", "%"),
        ("million", "m"),
        ("billion", "b"),
        ("thousand", "k"),
    ):
        if t.endswith(u):
            unit, t = name, t[: -len(u)].strip()
            break
    else:
        if t and t[-1] in "kmb" and t[:-1].replace(".", "").replace("-", "").isdigit():
            unit, t = t[-1], t[:-1]
    try:
        value = float(t)
    except ValueError:
        return None
    decimals = len(t.split(".")[1]) if "." in t else 0
    return value, decimals, unit


def numbers_in(text: str) -> list[str]:
    return [m.group(0).strip() for m in _NUM.finditer(text)]


def _close(claimed: float, decimals: int, actual: float) -> bool:
    if math.isclose(claimed, actual, rel_tol=1e-9, abs_tol=1e-9):
        return True
    # The answer may round: 3.14159 written as 3.14 or 3. Allow half a unit of the written precision,
    # and 0.5% relative slack for "about" style rounding of big numbers.
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    return abs(claimed - actual) <= tol or abs(claimed - actual) <= 0.005 * abs(actual)


def unverified_numbers(
    answer: str, rows: list[list[Any]], *, row_count: int, context: str = ""
) -> list[str]:
    cells = [
        float(v) for r in rows for v in r if isinstance(v, int | float) and not isinstance(v, bool)
    ]
    for r in rows:  # numbers inside text cells ("2017 Italian Grand Prix", "1980-01-01")
        for v in r:
            if isinstance(v, str):
                cells.extend(float(x) for x in re.findall(r"\d+(?:\.\d+)?", v))
    cells.append(float(row_count))
    known_text = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", context)}
    bad: list[str] = []
    for token in numbers_in(answer):
        parsed = _parse(token)
        if parsed is None:
            continue
        value, decimals, unit = parsed
        if value in known_text:
            continue
        scale = {"m": 1e6, "b": 1e9, "k": 1e3}.get(unit, 1.0)
        if scale != 1.0:  # "1.2 million": within half a unit of the last written digit
            step = scale * 10 ** (-decimals)
            ok = any(abs(value * scale - cell) <= 0.5 * step for cell in cells)
        elif unit == "%":  # "12.5%" may describe 12.5 or a 0.125 fraction
            ok = any(
                _close(value, decimals, cell) or _close(value, decimals, cell * 100)
                for cell in cells
            )
        else:
            ok = any(_close(value, decimals, cell) for cell in cells)
        if not ok:
            bad.append(token)
    return bad
