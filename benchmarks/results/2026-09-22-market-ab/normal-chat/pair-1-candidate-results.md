# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:51:30.114454+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `0f7eb9797d20483a84a98e49391e2b59`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:51:03.504662+00:00`
- Runtime: `26.6s`
- Pass rate: `4/4` (100.0%)
- Check score: `60/60` (100.0%)
- End-to-end latency: avg `6652ms`, p50 `6020ms`, p90 `9651ms`, max `9651ms`
- Agent averages: reply `6652ms`, turns `2.25`, tools `1.75`, tokens `8100`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,139 | final_text | - | 3.9s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist, quote | 3 | 9,426 | final_text | - | 7.1s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,726 | final_text | - | 6.0s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,107 | final_text | - | 9.7s |
