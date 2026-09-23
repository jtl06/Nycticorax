# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:52:23.901164+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `9f9480acd21644989ea9152430d4ea2d`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:51:58.283587+00:00`
- Runtime: `25.6s`
- Pass rate: `4/4` (100.0%)
- Check score: `60/60` (100.0%)
- End-to-end latency: avg `6404ms`, p50 `6409ms`, p90 `8203ms`, max `8203ms`
- Agent averages: reply `6404ms`, turns `2.25`, tools `1.75`, tokens `7990`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,072 | final_text | - | 4.6s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist, quote | 3 | 9,273 | final_text | - | 6.4s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,688 | final_text | - | 6.5s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 7,927 | final_text | - | 8.2s |
