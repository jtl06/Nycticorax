# Benchmark Results

Revision: `997fa4c + working tree`
Captured: `2026-10-05T04:24:30.186467+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `6b5986c7789946999efbefeebae93c36`
- Manifest version: `24`
- Mode: `fixtures`
- Started: `2026-10-05T04:24:08.756383+00:00`
- Runtime: `21.4s`
- Pass rate: `8/8` (100.0%)
- Check score: `118/118` (100.0%)
- End-to-end latency: avg `2679ms`, p50 `2691ms`, p90 `3950ms`, max `3950ms`
- Agent averages: reply `2678ms`, turns `1.62`, tools `0.75`, tokens `6032`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-watchlist-session-quote` | 1 | PASS | 22/22 | gpt-5.6-terra | openai | quote | 2 | 8,531 | final_text | - | 3.7s |
| `fixture-watchlist-market-correction` | 1 | PASS | 21/21 | gpt-5.6-terra | openai | quote, web | 2 | 9,341 | final_text | - | 4.0s |
| `fixture-watchlist-list-callback` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | quote | 2 | 8,348 | final_text | - | 3.9s |
| `fixture-watchlist-list-only` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | - | 1 | 3,694 | final_text | - | 1.1s |
| `fixture-watchlist-saying` | 1 | PASS | 9/9 | gpt-5.6-terra | openai | - | 1 | 3,638 | final_text | - | 1.4s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist | 2 | 5,984 | final_text | - | 3.0s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,778 | final_text | - | 1.7s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 5,946 | final_text | - | 2.7s |
