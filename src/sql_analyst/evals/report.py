"""Aggregate eval runs into `evals/results.json` and `evals/results.md`.

Accuracy comes with a 95% bootstrap confidence interval (2,000 resamples of questions), and each
configuration is compared with the baseline on the *same* questions (paired bootstrap of the
difference), so "B beats A" is only claimed when the interval excludes zero.
"""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path
from typing import Any

from .run import CONFIGS, RUNS

OUT_JSON = Path("evals") / "results.json"
OUT_MD = Path("evals") / "results.md"
_RNG_SEED = 7
_B = 2000


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ci(values: list[float]) -> tuple[float, float]:
    rng = random.Random(_RNG_SEED)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(_B))
    return means[int(0.025 * _B)], means[int(0.975 * _B) - 1]


def _pct(x: float) -> float:
    return round(100 * x, 1)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ex = [float(r["ex_set"]) for r in rows]
    lo, hi = _ci(ex)
    by_diff: dict[str, dict[str, Any]] = {}
    for d in ("simple", "moderate", "challenging"):
        sub = [r for r in rows if r["difficulty"] == d]
        if sub:
            by_diff[d] = {"n": len(sub), "ex": _pct(sum(r["ex_set"] for r in sub) / len(sub))}
    lat = sorted(r["wall_s"] for r in rows)
    retried = [r for r in rows if r["n_attempts"] > 1]
    rescued = [r for r in retried if r["ex_set"] and not r["first_ex_set"]]
    hurt = [r for r in retried if r["first_ex_set"] and not r["ex_set"]]
    recall = [r["link_recall"] for r in rows if r.get("link_recall") is not None]
    return {
        "n": n,
        "ex_set": _pct(sum(ex) / n),
        "ex_set_ci95": [_pct(lo), _pct(hi)],
        "ex_ordered": _pct(sum(r["ex_ordered"] for r in rows) / n),
        "ex_set_raw_floats": _pct(sum(r["ex_set_raw"] for r in rows) / n),
        "by_difficulty": by_diff,
        "exec_errors": sum(1 for r in rows if r["exec_error"]),
        "failed": sum(1 for r in rows if r["status"] == "failed"),
        "retried": len(retried),
        "rescued": len(rescued),
        "hurt": len(hurt),
        # Widening only means something when the prompt had a partial schema to begin with.
        "escalated": sum(1 for r in rows if r.get("escalated") and r.get("linked_tables")),
        "link_recall": _pct(sum(recall) / len(recall)) if recall else None,
        "mean_input_tokens": round(statistics.mean(r["input_tokens"] for r in rows)),
        "mean_output_tokens": round(statistics.mean(r["output_tokens"] for r in rows)),
        "mean_llm_calls": round(statistics.mean(r["llm_calls"] for r in rows), 2),
        "usd_per_question": round(statistics.mean(r["usd"] for r in rows), 6),
        "usd_total": round(sum(r["usd"] for r in rows), 4),
        "cached_calls": sum(r.get("cached_calls", 0) for r in rows),
        "latency_p50_s": round(lat[n // 2], 2),
        "latency_p95_s": round(lat[min(n - 1, int(0.95 * n))], 2),
    }


def paired_delta(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any] | None:
    """B minus A on the questions both runs answered."""
    ia = {r["question_id"]: r["ex_set"] for r in a}
    ib = {r["question_id"]: r["ex_set"] for r in b}
    common = sorted(ia.keys() & ib.keys())
    if not common:
        return None
    diffs = [float(ib[q]) - float(ia[q]) for q in common]
    lo, hi = _ci(diffs)
    return {
        "n": len(common),
        "delta_pp": _pct(sum(diffs) / len(diffs)),
        "ci95_pp": [_pct(lo), _pct(hi)],
        "b_only": sum(1 for d in diffs if d > 0),
        "a_only": sum(1 for d in diffs if d < 0),
    }


def build() -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    raw: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(RUNS.glob("*.jsonl")):
        name = path.stem
        rows = _load(path)
        if not rows:
            continue
        config, split_name = name.split("__")
        cfg = CONFIGS.get(config)
        if cfg and not cfg.agent.link_schema:
            for r in rows:  # early runs logged recall even when the whole schema was sent
                r["link_recall"] = None
        runs[name] = {
            "config": config,
            "split": split_name,
            "model": cfg.model if cfg else None,
            "effort": cfg.effort if cfg else None,
            "description": cfg.description if cfg else "",
            **summarize(rows),
        }
        raw[name] = rows
    comparisons = {}
    for name, rows in raw.items():
        config, split_name = name.split("__")
        base = raw.get(f"baseline__{split_name}")
        if config != "baseline" and base:
            comparisons[f"{config} vs baseline ({split_name})"] = paired_delta(base, rows)
    if "agent__test" in raw and "agent_sonnet__test" in raw:
        comparisons["agent_sonnet vs agent (test)"] = paired_delta(
            raw["agent__test"], raw["agent_sonnet__test"]
        )
    result = {"runs": runs, "comparisons": comparisons}
    OUT_JSON.write_text(json.dumps(result, indent=1) + "\n")
    OUT_MD.write_text(to_markdown(result))
    return result


def to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# BIRD mini-dev results",
        "",
        "Generated by `python -m sql_analyst.evals report` from `evals/runs/*.jsonl`.",
        "EX = execution accuracy (BIRD set comparison, floats rounded to 6 dp). CI = 95% bootstrap.",
        "",
        "| run | model | effort | N | EX | 95% CI | simple | moderate | challenging | EX ordered "
        "| calls/q | in tok/q | out tok/q | $/q | p50 s | p95 s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in result["runs"].items():
        d = r["by_difficulty"]

        def cell(k: str, d: dict[str, Any] = d) -> str:
            return f"{d[k]['ex']}% ({d[k]['n']})" if k in d else "-"

        lines.append(
            f"| {name} | {r['model']} | {r['effort']} | {r['n']} | **{r['ex_set']}%** | {r['ex_set_ci95'][0]}-"
            f"{r['ex_set_ci95'][1]} | {cell('simple')} | {cell('moderate')} | {cell('challenging')} "
            f"| {r['ex_ordered']}% | {r['mean_llm_calls']} | {r['mean_input_tokens']} "
            f"| {r['mean_output_tokens']} | ${r['usd_per_question']:.5f} | {r['latency_p50_s']} "
            f"| {r['latency_p95_s']} |"
        )
    lines += [
        "",
        "## Self-correction and linking",
        "",
        "| run | retried | rescued | hurt | escalated to full schema | link recall | failed |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, r in result["runs"].items():
        recall = f"{r['link_recall']}%" if r["link_recall"] is not None else "-"
        lines.append(
            f"| {name} | {r['retried']} | {r['rescued']} | {r['hurt']} | {r['escalated']} "
            f"| {recall} | {r['failed']} |"
        )
    lines += [
        "",
        "## Paired comparisons",
        "",
        "| comparison | N | delta | 95% CI | only B right | only A right |",
        "|---|---|---|---|---|---|",
    ]
    for k, c in result["comparisons"].items():
        if c:
            lines.append(
                f"| {k} | {c['n']} | {c['delta_pp']:+} pp | {c['ci95_pp'][0]:+} to "
                f"{c['ci95_pp'][1]:+} pp | {c['b_only']} | {c['a_only']} |"
            )
    return "\n".join(lines) + "\n"
