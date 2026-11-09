# 005: Claude Haiku 5.5 at medium effort through LangChain; no model review of results by default

- **Status:** accepted
- **Date:** 2026-11-08

## Context
The agent makes 1-3 model calls per question. Cost and latency matter (an analyst UI is interactive;
the project had a $5 total API budget), but so does accuracy. Three knobs were measured on the
**dev** split (N=100) before anything ran on test.

## Decision
1. **Model: Claude Haiku 5.5** ($0.10 / $0.50 per M tokens). Claude Sonnet 5.5 ($2 / $10) was run on a
   random 100-question subset of test for comparison: SONNET_RESULT
2. **Effort: medium** (adaptive thinking is on for Haiku 5.5; `output_config.effort` sets its depth).
   Dev, same pipelines, low vs medium:

   | config | low | medium |
   |---|---|---|
   | baseline (one shot) | 59% | 63% |
   | full schema + self-correction | 64% | 71% |
   | linking + self-correction | 63% | 70% |

   Medium was better in all three pairs, for ~20% more output tokens and ~0.4 s at the median.
3. **No result review by default.** A `review` node (a second call that reads the first 15 rows and
   can send the query back with a concrete problem) was built and measured. On dev at medium effort it
   rescued 8 questions and broke 2, but the run scored 69% vs 70% without it: no net gain inside the
   noise, while doubling calls (2.1 per question), prompt tokens (6.3k vs 3.2k) and p95 latency
   (18.1 s vs 9.5 s). It stays in the code behind `AgentConfig.review_result` for databases where
   wrong-shape answers are common.
4. **LangChain `ChatAnthropic.with_structured_output(method="json_schema")`** for every call: native
   structured output (`output_config.format`) validated again by pydantic, retries/timeouts from the
   client, and the owner's convention of model access through LangChain interfaces. The graph is
   LangGraph. No raw SDK calls.

## Alternatives considered
- **Forced tool use for structured output:** current Claude 5.5 models reject forced `tool_choice`;
  JSON-schema output is the supported path.
- **Sonnet as the default:** see the comparison above; at ~20x the price per token it has to buy a
  clear accuracy gain to be the default for an interactive tool.
- **Effort high:** not tried on the full dev set to stay inside the budget; medium already covers the
  interactive latency target (p50 ~4 s).

## Consequences
Per question, the production configuration costs well under a tenth of a cent on BIRD-sized schemas.
Accuracy on BIRD's hardest questions is limited by question interpretation, not by SQL syntax (most
failures run fine and return a plausible but different result), which neither retries nor a review
pass fixed reliably.
