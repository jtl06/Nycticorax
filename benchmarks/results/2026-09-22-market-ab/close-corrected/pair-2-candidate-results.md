# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:46:59.914273+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `c3b48f0255254bd0ba105834b2c1373d`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:46:46.710374+00:00`
- Runtime: `13.2s`
- Pass rate: `0/1` (0.0%)
- Check score: `11/12` (91.7%)
- End-to-end latency: avg `13204ms`, p50 `13204ms`, p90 `13204ms`, max `13204ms`
- Agent averages: reply `13203ms`, turns `4`, tools `2`, tokens `13754`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-explicit-close` | 1 | FAIL | 11/12 | gpt-5.6-terra | openai | price_hist, quote | 4 | 13,754 | final_text | - | 13.2s |

## Failures and errors

- `fixture-market-explicit-close` attempt 1: metric:max:agent_model_turn_count: observed 4; required at most 3
