# Benchmark Results

Revision: `8f40371 + working tree`
Captured: `2026-09-02T06:56:27.591918+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `61c9259fae364bb39b1db161fb6b6b99`
- Manifest version: `19`
- Mode: `fixtures`
- Started: `2026-09-02T06:54:16.765933+00:00`
- Runtime: `130.8s`
- Pass rate: `33/33` (100.0%)
- Check score: `437/437` (100.0%)
- End-to-end latency: avg `3964ms`, p50 `3010ms`, p90 `6344ms`, max `17583ms`
- Agent averages: reply `3964ms`, turns `1.79`, tools `0.88`, tokens `6522`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-quick-recursion` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,222 | final_text | - | 2.4s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,144 | final_text | - | 2.2s |
| `fixture-calculation` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | calc | 2 | 6,477 | final_text | - | 2.5s |
| `fixture-earnings-comparison` | 1 | PASS | 23/23 | gpt-5.6-terra | openai | web | 2 | 8,793 | final_text | - | 6.3s |
| `fixture-fresh-release` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | web | 2 | 7,032 | final_text | - | 3.0s |
| `fixture-fresh-news` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 7,108 | final_text | - | 3.3s |
| `fixture-opaque-version` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 6,681 | final_text | - | 3.6s |
| `fixture-url-policy` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 6,939 | final_text | - | 2.5s |
| `fixture-browser-dashboard` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | browser_extract, url_extract | 3 | 10,691 | final_text | - | 4.6s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 7,754 | final_text | - | 3.1s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 9,497 | final_text | - | 4.7s |
| `fixture-terse-stock-callback` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 8,054 | final_text | - | 2.9s |
| `fixture-full-market-scope` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote, web | 2 | 9,011 | final_text | - | 10.4s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 9,725 | final_text | - | 17.6s |
| `fixture-price-history` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | price_hist | 2 | 6,734 | final_text | - | 3.7s |
| `fixture-annual-performance` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | annual_perf | 2 | 7,794 | final_text | - | 2.8s |
| `fixture-transcript` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | yt_transcript | 2 | 7,046 | final_text | - | 3.6s |
| `fixture-image-search` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | img_search | 2 | 6,567 | final_text | - | 2.8s |
| `fixture-memory-private` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 6,579 | final_text | - | 2.8s |
| `fixture-memory-shared` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | memory_search | 2 | 6,580 | final_text | - | 2.8s |
| `fixture-memory-lore` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 6,585 | final_text | - | 3.2s |
| `fixture-memory-prefetch` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | - | 1 | 3,409 | final_text | - | 1.3s |
| `fixture-memory-named-shared-watchlist` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | - | 2 | 6,463 | final_text | - | 4.2s |
| `fixture-memory-temporal` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 3,348 | final_text | - | 1.5s |
| `fixture-channel-decision` | 1 | PASS | 24/24 | gpt-5.6-terra | openai | channel_ctx | 2 | 7,333 | final_text | - | 3.6s |
| `fixture-deep-comparison` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | deep_research | 2 | 7,653 | final_text | - | 8.2s |
| `fixture-composite-mixed` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | deep_research | 2 | 7,870 | final_text | - | 5.3s |
| `fixture-honest-missing-url` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 6,847 | final_text | - | 2.7s |
| `fixture-discord-reply-time` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,344 | final_text | - | 1.6s |
| `fixture-discord-correction` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,458 | final_text | - | 2.5s |
| `fixture-discord-summary` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 3,299 | final_text | - | 1.8s |
| `fixture-discord-topic-switch` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | calc | 2 | 6,739 | final_text | - | 4.2s |
| `fixture-discord-banter-recovery` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,441 | final_text | - | 3.0s |
