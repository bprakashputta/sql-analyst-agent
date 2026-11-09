"""Schema introspection and schema linking.

`introspect` reads tables, columns, primary and foreign keys once per database and samples a few
distinct values for every text column. `link` picks the tables a question needs, so the prompt carries a
few relevant tables instead of the whole catalog:

1. Score every column against the question (+ evidence): name tokens, table-name tokens, and sampled
   values that appear verbatim in the question ("Rock", "Brazil").
2. Keep the top `max_tables` tables with a positive score (always at least one).
3. Add the bridge tables on the foreign-key path between the kept tables, so the model can write the
   join (`invoice_line` between `track` and `invoice`).

The result renders as a compact schema card: CREATE-TABLE-like lines with types, keys, FKs and
sample values. ADR 003 has the measured recall of this linker on BIRD mini-dev.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_TOKEN = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "for",
        "to",
        "by",
        "with",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "how",
        "many",
        "much",
        "list",
        "show",
        "give",
        "find",
        "all",
        "each",
        "per",
        "from",
        "that",
        "this",
        "those",
        "these",
        "their",
        "there",
        "than",
        "as",
        "at",
        "it",
        "its",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "me",
        "my",
        "please",
        "number",
        "count",
        "total",
        "average",
        "sum",
        "name",
        "names",
        "among",
        "between",
    ]
)


def tokens(text: str) -> list[str]:
    """Lower-case word tokens with camelCase and snake_case split and a light plural strip."""
    out: list[str] = []
    for raw in _TOKEN.findall(_CAMEL.sub(" ", text).lower()):
        if raw in _STOP:
            continue
        out.append(_stem(raw))
    return out


def _stem(word: str) -> str:
    """A tiny plural stemmer: countries -> country, invoices -> invoice, sales -> sale."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


@dataclass
class Column:
    name: str
    type: str
    primary_key: bool = False
    nullable: bool = True
    samples: list[str] = field(default_factory=list)
    description: str | None = None
    # Lower-cased distinct values of a low-cardinality text column. Used for matching, never rendered.
    values: frozenset[str] = frozenset()


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class Table:
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    row_count: int | None = None


@dataclass
class Schema:
    dialect: str
    tables: dict[str, Table]

    @property
    def table_names(self) -> frozenset[str]:
        return frozenset(t.lower() for t in self.tables)

    def neighbors(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {t: set() for t in self.tables}
        lower = {t.lower(): t for t in self.tables}
        for t in self.tables.values():
            for fk in t.foreign_keys:
                ref = lower.get(fk.ref_table.lower())
                if ref and ref != t.name:
                    graph[t.name].add(ref)
                    graph[ref].add(t.name)
        return graph


def _quote(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f'"{name}"'


def introspect(
    engine: Engine,
    dialect: str,
    *,
    sample_values: int = 4,
    sample_scan: int = 2000,
    value_scan: int = 50_000,
    max_index_values: int = 1000,
) -> Schema:
    """Read the catalog.

    For each text column: `sample_values` frequent values (from the first `sample_scan` rows) go into the
    prompt; if the first `value_scan` rows hold at most `max_index_values` distinct values, all of them go
    into a match-only value index, so "Marvel Comics" in a question finds the `publisher` table.
    """
    insp = sa.inspect(engine)
    tables: dict[str, Table] = {}
    with engine.connect() as conn:
        for tname in insp.get_table_names():
            if tname.lower().startswith("sqlite_"):
                continue
            pk = set(insp.get_pk_constraint(tname).get("constrained_columns") or [])
            cols = [
                Column(
                    name=c["name"],
                    type=str(c["type"]) if str(c["type"]) != "NULL" else "",
                    primary_key=c["name"] in pk,
                    nullable=bool(c.get("nullable", True)),
                )
                for c in insp.get_columns(tname)
            ]
            fks = [
                ForeignKey(fk["constrained_columns"], fk["referred_table"], fk["referred_columns"])
                for fk in insp.get_foreign_keys(tname)
                if fk.get("referred_table")
            ]
            qt = _quote(tname)
            try:
                count = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {qt}").scalar()
            except sa.exc.DBAPIError:
                count = None
            for col in cols:
                if not _is_text(col.type) or col.primary_key:
                    continue
                qc = _quote(col.name)
                sql = (
                    f"SELECT v FROM (SELECT {qc} AS v FROM {qt} LIMIT {sample_scan}) s "
                    f"WHERE v IS NOT NULL AND v <> '' GROUP BY v ORDER BY COUNT(*) DESC LIMIT {sample_values}"
                )
                try:
                    col.samples = [
                        str(r[0])[:40]
                        for r in conn.exec_driver_sql(sql).fetchall()
                        if r[0] is not None
                    ]
                except sa.exc.DBAPIError:
                    col.samples = []
                sql = (
                    f"SELECT DISTINCT v FROM (SELECT {qc} AS v FROM {qt} LIMIT {value_scan}) s "
                    f"WHERE v IS NOT NULL LIMIT {max_index_values + 1}"
                )
                try:
                    vals = [str(r[0]) for r in conn.exec_driver_sql(sql).fetchall()]
                except sa.exc.DBAPIError:
                    vals = []
                if len(vals) <= max_index_values:
                    col.values = frozenset(
                        v.lower().strip() for v in vals if 3 <= len(v.strip()) <= 60
                    )
            tables[tname] = Table(tname, cols, fks, count)
    return Schema(dialect=dialect, tables=tables)


def _is_text(type_name: str) -> bool:
    t = type_name.upper()
    return t == "" or any(k in t for k in ("CHAR", "TEXT", "CLOB", "STRING"))


# -- linking ---------------------------------------------------------------------------------------


@dataclass
class LinkResult:
    tables: list[str]
    scores: dict[str, float]
    bridges: list[str]


def _ngrams(text: str, n_max: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9'.&-]*", text)
    words = [w.strip(".'") for w in words]
    return {
        " ".join(words[i : i + n]) for n in range(1, n_max + 1) for i in range(len(words) - n + 1)
    }


def _column_score(
    q_tokens: set[str], q_text: str, table: Table, col: Column, q_grams: set[str] | None = None
) -> float:
    score = 0.0
    name_toks = set(tokens(col.name))
    if name_toks:
        overlap = len(name_toks & q_tokens)
        score += 2.0 * overlap / len(name_toks) + 0.5 * overlap
    if col.description:
        score += 0.3 * len(set(tokens(col.description)) & q_tokens)
    grams = q_grams if q_grams is not None else _ngrams(q_text)
    candidates = col.values or {s.lower().strip() for s in col.samples}
    if any(v in grams for v in candidates if len(v) >= 3 and not v.isdigit()):
        score += 3.0
    return score


def link(schema: Schema, question: str, evidence: str = "", *, max_tables: int = 6) -> LinkResult:
    text = f"{question} {evidence}".lower()
    q_tokens = set(tokens(text))
    q_grams = _ngrams(text)
    scores: dict[str, float] = {}
    for t in schema.tables.values():
        t_toks = set(tokens(t.name))
        t_score = 3.0 * len(t_toks & q_tokens) / max(len(t_toks), 1)
        col_scores = sorted(
            (_column_score(q_tokens, text, t, c, q_grams) for c in t.columns), reverse=True
        )
        scores[t.name] = t_score + sum(col_scores[:3])
    ranked = sorted(scores, key=lambda n: (-scores[n], n))
    chosen = [n for n in ranked[:max_tables] if scores[n] > 0] or ranked[:1]

    bridges = _bridges(schema, chosen)
    return LinkResult(
        tables=chosen + [b for b in bridges if b not in chosen], scores=scores, bridges=bridges
    )


def _bridges(schema: Schema, chosen: list[str]) -> list[str]:
    """Tables on the shortest FK path from the first chosen table to each of the others."""
    graph = schema.neighbors()
    if len(chosen) < 2:
        return []
    extra: list[str] = []
    root = chosen[0]
    for target in chosen[1:]:
        prev: dict[str, str | None] = {root: None}
        queue = deque([root])
        while queue:
            node = queue.popleft()
            if node == target:
                break
            for nb in graph[node]:
                if nb not in prev:
                    prev[nb] = node
                    queue.append(nb)
        if target not in prev:
            continue
        node = prev[target]
        while node is not None and node != root:
            if node not in chosen and node not in extra:
                extra.append(node)
            node = prev[node]
    return extra


# -- rendering -------------------------------------------------------------------------------------


def render(
    schema: Schema, table_names: list[str] | None = None, *, with_samples: bool = True
) -> str:
    """A compact schema card for the prompt."""
    names = table_names if table_names is not None else list(schema.tables)
    blocks: list[str] = []
    for name in names:
        t = schema.tables[name]
        lines = [
            f"TABLE {_quote(t.name)}"
            + (f"  -- {t.row_count} rows" if t.row_count is not None else "")
        ]
        for c in t.columns:
            bits = [f"  {_quote(c.name)} {c.type}".rstrip()]
            if c.primary_key:
                bits.append("PRIMARY KEY")
            notes = []
            if c.description:
                notes.append(c.description)
            if with_samples and c.samples:
                notes.append("e.g. " + ", ".join(repr(s) for s in c.samples))
            line = " ".join(bits)
            if notes:
                line += "  -- " + "; ".join(notes)
            lines.append(line)
        for fk in t.foreign_keys:
            lines.append(
                f"  FOREIGN KEY ({', '.join(fk.columns)}) REFERENCES {fk.ref_table}({', '.join(fk.ref_columns)})"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
