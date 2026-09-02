# Benchmark Results

Revision: `c0f936c + working tree`
Captured: `2026-09-02T05:26:33.444505+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `025d0d900ba94d70b378353cd1fcdfe4`
- Manifest version: `19`
- Mode: `fixtures`
- Started: `2026-09-02T05:24:23.989253+00:00`
- Runtime: `129.5s`
- Pass rate: `31/33` (93.9%)
- Check score: `433/437` (99.1%)
- End-to-end latency: avg `3923ms`, p50 `3065ms`, p90 `7561ms`, max `15257ms`
- Agent averages: reply `3922ms`, turns `1.82`, tools `0.85`, tokens `7721`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-quick-recursion` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,968 | final_text | - | 2.5s |
| `fixture-social-banter` | 1 | FAIL | 8/10 | gpt-5.6-terra | openai | - | 2 | 6,155 | final_text | - | 3.4s |
| `fixture-calculation` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | calc | 2 | 8,033 | final_text | - | 2.6s |
| `fixture-earnings-comparison` | 1 | PASS | 23/23 | gpt-5.6-terra | openai | deep_research | 2 | 9,397 | final_text | - | 7.6s |
| `fixture-fresh-release` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | web | 2 | 8,223 | final_text | - | 3.3s |
| `fixture-fresh-news` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 8,249 | final_text | - | 2.8s |
| `fixture-opaque-version` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 8,245 | final_text | - | 3.6s |
| `fixture-url-policy` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 8,163 | final_text | - | 3.1s |
| `fixture-browser-dashboard` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | browser_extract, url_extract | 3 | 12,384 | final_text | - | 4.4s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 8,183 | final_text | - | 3.2s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote | 2 | 9,081 | final_text | - | 4.5s |
| `fixture-terse-stock-callback` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 8,557 | final_text | - | 3.1s |
| `fixture-full-market-scope` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote, web | 2 | 9,502 | final_text | - | 11.7s |
| `fixture-overnight-watchlist` | 1 | FAIL | 16/18 | gpt-5.6-terra | openai | channel_ctx, quote, web | 3 | 16,463 | final_text | - | 15.3s |
| `fixture-price-history` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | price_hist | 2 | 8,251 | final_text | - | 3.5s |
| `fixture-annual-performance` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | annual_perf | 2 | 8,347 | final_text | - | 4.5s |
| `fixture-transcript` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | yt_transcript | 2 | 8,229 | final_text | - | 3.3s |
| `fixture-image-search` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | img_search | 2 | 8,117 | final_text | - | 3.1s |
| `fixture-memory-private` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 8,213 | final_text | - | 3.0s |
| `fixture-memory-shared` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | memory_search | 2 | 8,212 | final_text | - | 2.8s |
| `fixture-memory-lore` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 8,170 | final_text | - | 3.0s |
| `fixture-memory-prefetch` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | - | 1 | 4,221 | final_text | - | 1.5s |
| `fixture-memory-named-shared-watchlist` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | - | 1 | 4,254 | final_text | - | 1.1s |
| `fixture-memory-temporal` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 4,163 | final_text | - | 1.3s |
| `fixture-channel-decision` | 1 | PASS | 24/24 | gpt-5.6-terra | openai | channel_ctx | 2 | 8,654 | final_text | - | 3.5s |
| `fixture-deep-comparison` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | deep_research | 2 | 9,322 | final_text | - | 9.4s |
| `fixture-composite-mixed` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | deep_research | 2 | 9,161 | final_text | - | 5.8s |
| `fixture-honest-missing-url` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 8,010 | final_text | - | 3.0s |
| `fixture-discord-reply-time` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 4,116 | final_text | - | 1.4s |
| `fixture-discord-correction` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 4,233 | final_text | - | 1.9s |
| `fixture-discord-summary` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 4,089 | final_text | - | 1.6s |
| `fixture-discord-topic-switch` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | calc | 2 | 8,295 | final_text | - | 3.1s |
| `fixture-discord-banter-recovery` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 4,140 | final_text | - | 1.7s |

## Failures and errors

- `fixture-social-banter` attempt 1: metric:max:agent_model_turn_count: observed 2; required at most 1; metric:max:agent_total_tokens: observed 6155; required at most 6000
- `fixture-overnight-watchlist` attempt 1: answer:forbidden:1: forbidden pattern '\\bGTS\\b' was found; tool:not_called:channel_ctx: channel_ctx was called
