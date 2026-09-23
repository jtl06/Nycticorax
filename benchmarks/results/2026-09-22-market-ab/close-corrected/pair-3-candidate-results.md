# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:47:25.984768+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `880bb961f7b04788811cc9f8da34f0be`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:47:17.870172+00:00`
- Runtime: `8.1s`
- Pass rate: `0/1` (0.0%)
- Check score: `11/12` (91.7%)
- End-to-end latency: avg `8115ms`, p50 `8115ms`, p90 `8115ms`, max `8115ms`
- Agent averages: reply `8114ms`, turns `4`, tools `2`, tokens `12954`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-explicit-close` | 1 | FAIL | 11/12 | gpt-5.6-terra | openai | price_hist, quote | 4 | 12,954 | final_text | - | 8.1s |

## Failures and errors

- `fixture-market-explicit-close` attempt 1: metric:max:agent_model_turn_count: observed 4; required at most 3
