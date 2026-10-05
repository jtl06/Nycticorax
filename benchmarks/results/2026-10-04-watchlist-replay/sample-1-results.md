# Benchmark Results

Revision: `997fa4c + working tree`
Captured: `2026-10-05T04:23:42.897310+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `ecd62e59120043f0a9435d69da9f387d`
- Manifest version: `24`
- Mode: `fixtures`
- Started: `2026-10-05T04:23:15.987018+00:00`
- Runtime: `26.9s`
- Pass rate: `8/8` (100.0%)
- Check score: `118/118` (100.0%)
- End-to-end latency: avg `3364ms`, p50 `2866ms`, p90 `6723ms`, max `6723ms`
- Agent averages: reply `3363ms`, turns `1.62`, tools `0.75`, tokens `5935`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-watchlist-session-quote` | 1 | PASS | 22/22 | gpt-5.6-terra | openai | quote | 2 | 8,521 | final_text | - | 6.7s |
| `fixture-watchlist-market-correction` | 1 | PASS | 21/21 | gpt-5.6-terra | openai | quote, web | 2 | 8,547 | final_text | - | 4.0s |
| `fixture-watchlist-list-callback` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | quote | 2 | 8,339 | final_text | - | 4.1s |
| `fixture-watchlist-list-only` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | - | 1 | 3,694 | final_text | - | 1.6s |
| `fixture-watchlist-saying` | 1 | PASS | 9/9 | gpt-5.6-terra | openai | - | 1 | 3,636 | final_text | - | 1.8s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist | 2 | 6,020 | final_text | - | 3.8s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,775 | final_text | - | 2.0s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 5,945 | final_text | - | 2.9s |
