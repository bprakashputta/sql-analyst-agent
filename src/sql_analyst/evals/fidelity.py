"""Guard fidelity: the guard re-renders every query through sqlglot. Does that ever change a result?

Runs all 500 BIRD gold queries twice, as written and as re-rendered by the guard, and compares the
results exactly (ordered, no float rounding). Writes `evals/guard_fidelity.json`. No model calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..db import Database
from ..guard import check_sql
from ..schema import introspect
from .bird import load_questions

OUT = Path("evals") / "guard_fidelity.json"


def main() -> None:
    questions = load_questions()
    dbs: dict[str, tuple[Database, frozenset[str]]] = {}
    refused, changed = [], []
    for q in questions:
        if q.db_id not in dbs:
            db = Database.sqlite(q.db_path, timeout_ms=60_000)
            dbs[q.db_id] = (db, introspect(db.meta_engine, "sqlite").table_names)
        db, tables = dbs[q.db_id]
        g = check_sql(q.gold_sql, dialect="sqlite", allowed_tables=tables, row_limit=10_000_000)
        if not g.ok:
            refused.append({"question_id": q.question_id, "reason": g.reason})
            continue
        a = db.run(q.gold_sql, max_rows=10_000_000).rows
        b = db.run(g.sql or "", max_rows=10_000_000).rows
        if a != b:
            changed.append({"question_id": q.question_id, "guarded_sql": g.sql})
    result = {"n": len(questions), "refused": refused, "result_changed": changed}
    OUT.write_text(json.dumps(result, indent=1) + "\n")
    print(
        f"{len(questions)} gold queries: {len(refused)} refused, {len(changed)} changed by re-rendering"
    )


if __name__ == "__main__":
    main()
