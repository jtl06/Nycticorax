# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:51:57.093115+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `e6f52bef2ff9463d91e5f24ee2b99033`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:51:31.211368+00:00`
- Runtime: `25.9s`
- Pass rate: `4/4` (100.0%)
- Check score: `60/60` (100.0%)
- End-to-end latency: avg `6470ms`, p50 `3900ms`, p90 `14492ms`, max `14492ms`
- Agent averages: reply `6470ms`, turns `2`, tools `1.25`, tokens `7128`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,116 | final_text | - | 3.9s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist | 2 | 5,980 | final_text | - | 3.3s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote | 2 | 7,884 | final_text | - | 4.2s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,531 | final_text | - | 14.5s |
