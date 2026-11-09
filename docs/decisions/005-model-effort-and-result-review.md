# 005: Sonnet 5.5 by default (Haiku 5.5 as budget mode), medium effort, no result review pass

- **Status:** accepted (revised after the test-split comparison)
- **Date:** 2026-11-09

## Context
The agent makes 1-3 model calls per question. Cost and latency matter (an analyst UI is interactive;
this project had a $5 total API budget for development and evaluation), but a wrong number delivered
confidently is the expensive failure. All iteration ran on Claude Haiku 5.5 to stay in budget, with a
planned comparison against Sonnet 5.5.

## Decision
1. **Default model: Claude Sonnet 5.5; Claude Haiku 5.5 is the budget mode** (`MODEL=`). On the same
   100 randomly sampled test questions, same pipeline, effort medium, no response cache:

   | | Haiku 5.5 | Sonnet 5.5 |
   |---|---|---|
   | execution accuracy (paired, N=100) | 69% | **87%** (+18 pp, 95% CI +8 to +28) |
   | cost per question | $0.00068 | $0.0063 |
   | output tokens per question | 1,192 | 390 |
   | latency p50 / p95 | 4.4 s / 14.8 s | **2.8 s / 8.1 s** |

   Sonnet costs ~9x more per question but is under a cent, *faster* (it thinks less at the same effort:
   a third of the output tokens), and right on 18 more questions in 100. For a tool whose failure mode
   is a confident wrong number, that is the better default. All headline Haiku numbers (N=400) are
   still reported, since they are the budget configuration.
2. **Effort: medium.** Dev split (N=100), Haiku, same pipelines, low vs medium:

   | config | low | medium |
   |---|---|---|
   | baseline (one shot) | 59% | 63% |
   | full schema + self-correction | 64% | 71% |
   | linking + self-correction | 63% | 70% |

   Medium was better in all three pairs, for ~20% more output tokens and +0.3-0.4 s at the median.
3. **No result-review pass by default.** A `review` node (a second call that reads the first 15 rows
   and can send the query back with a concrete problem) was built and measured on dev at medium
   effort: it rescued 8 questions and broke 2, and the run scored 69% vs 70% without it: no net gain
   within the noise, for double the calls (2.1 per question), prompt tokens (6.3k vs 3.2k) and p95
   latency (18.1 s vs 9.5 s). It stays behind `AgentConfig.review_result`.
4. **LangChain `ChatAnthropic.with_structured_output(method="json_schema")`** for every call: native
   structured output (`output_config.format`), validated again with pydantic; client retries and
   timeouts; model access through LangChain interfaces (the owner's convention), graph in LangGraph.

## Alternatives considered
- **Haiku by default** (the cheapest capable model): measured, and 18 points less accurate on the
  paired sample. Kept as an option for high-volume, low-stakes use.
- **Forced tool use for structured output:** current Claude 5.5 models reject forced `tool_choice`;
  JSON-schema output is the supported path.
- **Effort high / Opus:** not run, to stay inside the $5 budget.

## Consequences
At Sonnet prices a question costs about $0.006 (~$6 per 1,000 questions) on BIRD-sized schemas. The
Sonnet number has a wide interval (N=100); a bigger sample would narrow it. Most remaining Haiku
failures are interpretation, not syntax: 103 of its 113 wrong test answers ran cleanly and returned a
plausible but different result, which neither retries nor the review pass fixed reliably.
