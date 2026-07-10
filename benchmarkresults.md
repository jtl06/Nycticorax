# Benchmark Results

Run date: 2026-07-10

These runs exercised Nycti's normal `_generate_reply` path with the configured live
providers and tools. They used a temporary local SQLite database and did not post to
Discord or modify production state.

| Benchmark | Score | Runtime | Summary |
| --- | ---: | ---: | --- |
| Earnings | 9/10 | 21.5s | Correct comparison; 3 model turns and 2 fixture-tool calls. |
| Context | 7/8 | 12.1s | Retrieved the older channel context, but unnecessarily mentioned the superseded plan. |
| SpaceX price | 6/6 | 43.4s | Passed, but used 6 turns and 3 tool calls before reaching the agent deadline. |
| Semis | 2/6 | 30.4s | Failed before any quote or web call because the provider fallback timed out. |

## Findings

- The configured primary GPT-5.6 Luna provider failed every foreground turn in these
  runs. Nycti then attempted the configured DeepInfra DeepSeek V4 Pro fallback.
- This Luna behavior is known; DeepSeek V4 Pro is the intended runtime for these
  snapshots. A successful fallback is therefore not counted as a benchmark defect.
- Earnings, context, and SpaceX recovered through the fallback. Semis did not: its
  fallback call timed out before the agent could route to quote or web tools, producing
  Nycti's generic failure reply.
- The SpaceX canary passed functionally but has poor latency. Its 43.4-second total
  included six model turns, three tool calls, and a deadline stop.
- The earnings response was substantively correct. Its missing scorer point is a
  formatting issue: the scorer does not recognize valid `$10.253 billion` revenue and
  `~$11.2 billion` guidance phrasing as its AMD completeness patterns.
- The context response found the final deployment details but repeated the earlier
  proposal. The benchmark correctly penalizes that because the prompt asks for the
  final plan rather than its history.

## Captured Run Telemetry

All foreground runs initially attempted the configured `gpt-5.6-luna` primary model.
It failed in every observed foreground attempt. Successful runs continued through the
configured DeepInfra `deepseek-ai/DeepSeek-V4-Pro` fallback.

### Earnings

```text
score: completeness=9/10, correctness_checks=9/10
active_chat_model: deepseek-ai/DeepSeek-V4-Pro
primary_provider_failures: 3
agent_model_turn_count: 3
agent_tool_call_count: 2
tool_call_count: 2
agent_stop_reason: final_text
chat_total_tokens: 13,222
chat_llm_ms: 21,444
web_search_ms: 0 (deterministic benchmark fixture)
end_to_end_ms: 21,466
```

### Context

```text
score: 7/8
active_chat_model: deepseek-ai/DeepSeek-V4-Pro
primary_provider_failures: 2
agent_model_turn_count: 2
agent_tool_call_count: 1
tool_call_count: 1 (channel context)
agent_stop_reason: final_text
chat_total_tokens: 8,440
end_to_end_ms: 12,069
```

### SpaceX Price

```text
score: 6/6
active_chat_model: deepseek-ai/DeepSeek-V4-Pro
primary_provider_failures: 6
agent_model_turn_count: 6
agent_tool_call_count: 3
tool_call_count: 3
web_search_query_count: 4
quote_count: 1
agent_stop_reason: deadline
chat_total_tokens: 32,914
chat_llm_ms: 39,073
web_search_ms: 4,083
quote_ms: 168
end_to_end_ms: 43,355
```

### Semis

```text
score: 2/6
active_chat_model: gpt-5.6-luna
agent_model_turn_count: 0
agent_tool_call_count: 0
tool_call_count: 0
web_search_query_count: 0
quote_count: 0
agent_stop_reason: provider_error
chat_llm_ms: 30,407
end_to_end_ms: 30,437
failure_path: primary provider failure -> DeepInfra fallback APITimeoutError
```

The semis failure generated no model/tool trace because both provider attempts failed
before Nycti received a usable assistant turn. The missing trace is itself useful: the
runtime spent its full foreground budget on provider recovery instead of grounding.

## Verification

`PYTHONPATH=src python3 -m pytest tests/test_benchmarks.py -q` passed: 20 tests.

## Priorities

1. Bound DeepSeek V4 Pro latency so a stalled model leaves enough time for tool
   routing and a concise final response.
2. Reduce unnecessary model turns in the SpaceX/research loop.
3. Make the earnings scorer accept equivalent decimal and approximation formatting.
