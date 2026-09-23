# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:52:52.611436+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `4eb840d3b91e4a5db1b46752baf5c537`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:52:25.238091+00:00`
- Runtime: `27.4s`
- Pass rate: `3/4` (75.0%)
- Check score: `59/60` (98.3%)
- End-to-end latency: avg `6844ms`, p50 `4614ms`, p90 `13897ms`, max `13897ms`
- Agent averages: reply `6843ms`, turns `2`, tools `1.5`, tokens `7155`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,131 | final_text | - | 4.6s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist | 2 | 6,047 | final_text | - | 4.1s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote | 2 | 7,886 | final_text | - | 4.8s |
| `fixture-overnight-watchlist` | 1 | FAIL | 17/18 | gpt-5.6-terra | openai | memory_search, quote, web | 2 | 8,556 | final_text | - | 13.9s |

## Failures and errors

- `fixture-overnight-watchlist` attempt 1: tool:not_called:memory_search: memory_search was called
