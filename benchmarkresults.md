# Benchmark Results

Revision: `118e87f + working tree`
Captured: `2026-09-02T19:23:11.842593+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `6c98e2e07b9447c7874dd7c373e51db2`
- Manifest version: `19`
- Mode: `fixtures`
- Started: `2026-09-02T19:20:40.141509+00:00`
- Runtime: `151.7s`
- Pass rate: `33/33` (100.0%)
- Check score: `437/437` (100.0%)
- End-to-end latency: avg `4597ms`, p50 `3375ms`, p90 `8560ms`, max `20330ms`
- Agent averages: reply `4596ms`, turns `1.76`, tools `0.91`, tokens `6445`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-quick-recursion` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,169 | final_text | - | 4.1s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,123 | final_text | - | 2.0s |
| `fixture-calculation` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | calc | 2 | 6,443 | final_text | - | 3.2s |
| `fixture-earnings-comparison` | 1 | PASS | 23/23 | gpt-5.6-terra | openai | deep_research | 2 | 9,009 | final_text | - | 8.6s |
| `fixture-fresh-release` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | web | 2 | 7,027 | final_text | - | 3.3s |
| `fixture-fresh-news` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 6,968 | final_text | - | 3.0s |
| `fixture-opaque-version` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 6,648 | final_text | - | 3.8s |
| `fixture-url-policy` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 6,899 | final_text | - | 4.0s |
| `fixture-browser-dashboard` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | browser_extract, url_extract | 3 | 10,602 | final_text | - | 5.2s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 7,730 | final_text | - | 4.0s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 9,509 | final_text | - | 7.0s |
| `fixture-terse-stock-callback` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 8,045 | final_text | - | 3.2s |
| `fixture-full-market-scope` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote, web | 2 | 9,664 | final_text | - | 20.3s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 9,564 | final_text | - | 16.6s |
| `fixture-price-history` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | price_hist | 2 | 6,691 | final_text | - | 3.4s |
| `fixture-annual-performance` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | annual_perf | 2 | 7,759 | final_text | - | 3.5s |
| `fixture-transcript` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | yt_transcript | 2 | 6,961 | final_text | - | 3.8s |
| `fixture-image-search` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | img_search | 2 | 6,535 | final_text | - | 2.9s |
| `fixture-memory-private` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 6,551 | final_text | - | 3.4s |
| `fixture-memory-shared` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | memory_search | 2 | 6,546 | final_text | - | 3.5s |
| `fixture-memory-lore` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 6,547 | final_text | - | 3.2s |
| `fixture-memory-prefetch` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | - | 1 | 3,394 | final_text | - | 2.0s |
| `fixture-memory-named-shared-watchlist` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | - | 1 | 4,066 | final_text | - | 1.6s |
| `fixture-memory-temporal` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 3,336 | final_text | - | 1.4s |
| `fixture-channel-decision` | 1 | PASS | 24/24 | gpt-5.6-terra | openai | channel_ctx | 2 | 7,329 | final_text | - | 4.1s |
| `fixture-deep-comparison` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | deep_research | 2 | 7,688 | final_text | - | 9.8s |
| `fixture-composite-mixed` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | deep_research | 2 | 7,982 | final_text | - | 6.3s |
| `fixture-honest-missing-url` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 6,770 | final_text | - | 2.9s |
| `fixture-discord-reply-time` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,322 | final_text | - | 1.4s |
| `fixture-discord-correction` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,435 | final_text | - | 2.4s |
| `fixture-discord-summary` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 3,283 | final_text | - | 2.1s |
| `fixture-discord-topic-switch` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | calc | 2 | 6,707 | final_text | - | 2.8s |
| `fixture-discord-banter-recovery` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,374 | final_text | - | 2.8s |
