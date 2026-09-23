# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:51:02.138586+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `55e4bf9a8e4640a9a7a5bc5af25d9deb`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:50:30.951692+00:00`
- Runtime: `31.2s`
- Pass rate: `3/4` (75.0%)
- Check score: `59/60` (98.3%)
- End-to-end latency: avg `7797ms`, p50 `4479ms`, p90 `16937ms`, max `16937ms`
- Agent averages: reply `7796ms`, turns `2`, tools `1.75`, tokens `7435`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,016 | final_text | - | 4.5s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist | 2 | 6,027 | final_text | - | 4.3s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,580 | final_text | - | 5.4s |
| `fixture-overnight-watchlist` | 1 | FAIL | 17/18 | gpt-5.6-terra | openai | channel_ctx, quote, web | 2 | 9,118 | final_text | - | 16.9s |

## Failures and errors

- `fixture-overnight-watchlist` attempt 1: tool:not_called:channel_ctx: channel_ctx was called
