# Benchmark Results

Revision: `997fa4c + working tree`
Captured: `2026-10-05T04:24:07.506204+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `66b5c23a52ac40698672aacb52f3d71a`
- Manifest version: `24`
- Mode: `fixtures`
- Started: `2026-10-05T04:23:44.063405+00:00`
- Runtime: `23.4s`
- Pass rate: `8/8` (100.0%)
- Check score: `118/118` (100.0%)
- End-to-end latency: avg `2930ms`, p50 `3151ms`, p90 `4838ms`, max `4838ms`
- Agent averages: reply `2930ms`, turns `1.75`, tools `0.88`, tokens `6340`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-watchlist-session-quote` | 1 | PASS | 22/22 | gpt-5.6-terra | openai | quote | 2 | 8,531 | final_text | - | 4.0s |
| `fixture-watchlist-market-correction` | 1 | PASS | 21/21 | gpt-5.6-terra | openai | quote, web | 2 | 8,549 | final_text | - | 3.6s |
| `fixture-watchlist-list-callback` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | quote | 2 | 8,236 | final_text | - | 3.2s |
| `fixture-watchlist-list-only` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | - | 1 | 3,694 | final_text | - | 1.1s |
| `fixture-watchlist-saying` | 1 | PASS | 9/9 | gpt-5.6-terra | openai | - | 1 | 3,636 | final_text | - | 1.6s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist, quote | 3 | 9,381 | final_text | - | 4.8s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,774 | final_text | - | 1.9s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 5,917 | final_text | - | 3.2s |
