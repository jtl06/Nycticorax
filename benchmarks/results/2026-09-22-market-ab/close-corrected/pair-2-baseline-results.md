# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:47:11.594596+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `98fef92ce25f4d0fb13ffc3997cc1123`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:47:01.353987+00:00`
- Runtime: `10.2s`
- Pass rate: `0/1` (0.0%)
- Check score: `11/12` (91.7%)
- End-to-end latency: avg `10241ms`, p50 `10241ms`, p90 `10241ms`, max `10241ms`
- Agent averages: reply `10240ms`, turns `4`, tools `2`, tokens `13185`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-explicit-close` | 1 | FAIL | 11/12 | gpt-5.6-terra | openai | price_hist, quote | 4 | 13,185 | final_text | - | 10.2s |

## Failures and errors

- `fixture-market-explicit-close` attempt 1: metric:max:agent_model_turn_count: observed 4; required at most 3
