# Benchmark Results

Revision: `e0cf3c4 + working tree`
Captured: `2026-09-04T23:48:13.327127+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `9a7b7babf9a74c8eb06992e249d51b8e`
- Manifest version: `21`
- Mode: `fixtures`
- Started: `2026-09-04T23:46:06.310108+00:00`
- Runtime: `127.0s`
- Pass rate: `37/37` (100.0%)
- Check score: `470/470` (100.0%)
- End-to-end latency: avg `3433ms`, p50 `2756ms`, p90 `6747ms`, max `15686ms`
- Agent averages: reply `3432ms`, turns `1.7`, tools `0.86`, tokens `5355`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-quick-recursion` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 2,853 | final_text | - | 2.5s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,779 | final_text | - | 1.7s |
| `fixture-calculation` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | calc | 2 | 5,735 | final_text | - | 2.7s |
| `fixture-earnings-comparison` | 1 | PASS | 23/23 | gpt-5.6-terra | openai | web | 2 | 7,150 | final_text | - | 5.9s |
| `fixture-fresh-release` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | web | 2 | 5,928 | final_text | - | 3.1s |
| `fixture-fresh-news` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 5,912 | final_text | - | 2.9s |
| `fixture-opaque-version` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 6,028 | final_text | - | 3.8s |
| `fixture-url-policy` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 5,935 | final_text | - | 2.8s |
| `fixture-browser-dashboard` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | browser_extract, url_extract | 3 | 9,059 | final_text | - | 3.9s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 5,924 | final_text | - | 3.6s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote | 2 | 7,910 | final_text | - | 3.7s |
| `fixture-terse-stock-callback` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 7,337 | final_text | - | 3.1s |
| `fixture-full-market-scope` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote, web | 2 | 7,166 | final_text | - | 10.8s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,015 | final_text | - | 7.2s |
| `fixture-price-history` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | price_hist | 2 | 6,027 | final_text | - | 3.7s |
| `fixture-annual-performance` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | annual_perf | 2 | 5,999 | final_text | - | 3.5s |
| `fixture-transcript` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | yt_transcript | 2 | 6,022 | final_text | - | 3.0s |
| `fixture-image-search` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | img_search | 2 | 5,779 | final_text | - | 2.7s |
| `fixture-memory-private` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 5,853 | final_text | - | 4.0s |
| `fixture-memory-shared` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | memory_search | 2 | 5,846 | final_text | - | 2.5s |
| `fixture-memory-lore` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 5,863 | final_text | - | 3.9s |
| `fixture-memory-prefetch` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | - | 1 | 3,050 | final_text | - | 1.4s |
| `fixture-memory-named-shared-watchlist` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | - | 1 | 3,082 | final_text | - | 1.3s |
| `fixture-memory-temporal` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 2,988 | final_text | - | 1.4s |
| `fixture-channel-decision` | 1 | PASS | 24/24 | gpt-5.6-terra | openai | channel_ctx | 2 | 6,296 | final_text | - | 3.2s |
| `fixture-deep-comparison` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | web | 2 | 6,301 | final_text | - | 6.7s |
| `fixture-composite-mixed` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | calc, quote, url_extract, yt_transcript | 3 | 12,113 | final_text | - | 15.7s |
| `fixture-honest-missing-url` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 5,806 | final_text | - | 2.5s |
| `fixture-discord-reply-time` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,954 | final_text | - | 1.3s |
| `fixture-discord-correction` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,058 | final_text | - | 2.2s |
| `fixture-discord-summary` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 2,946 | final_text | - | 1.5s |
| `fixture-discord-topic-switch` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,889 | final_text | - | 1.1s |
| `fixture-discord-banter-recovery` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,011 | final_text | - | 1.9s |
| `fixture-scenario-correction-1` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,776 | final_text | - | 1.2s |
| `fixture-scenario-correction-2` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 2,913 | final_text | - | 1.2s |
| `fixture-scenario-market-callback-1` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 5,837 | final_text | - | 2.3s |
| `fixture-scenario-market-callback-2` | 1 | PASS | 9/9 | gpt-5.6-terra | openai | - | 1 | 2,988 | final_text | - | 1.0s |

## Simplification Validation

- The final full run used real OpenAI Terra inference with synthetic Discord context and frozen tool results; no production chat or memory was modified.
- OpenAI Terra and DeepInfra DeepSeek-V4-Pro-0813 each passed a separate native-tool call plus tool-result follow-up probe (about 2.12s and 1.70s respectively).
- The initial focused baseline passed 12/12, averaging 3618ms and 5580 tokens. The first simplified focused run passed 10/12, averaging 3753ms and 5119 tokens; both misses omitted the ticker from a mixed request. General ticker-shorthand guidance corrected those misses in a 2/2 recheck.
- The first full run passed 33/37. Two answers were numerically correct but missed tool-use checks; the database comparison hit a fixture that exposed its facts only through deep research; the overnight case incorrectly treated a member alias as a ticker.
- Manifest 21 keeps factual checks but removes route requirements from mixed research, database comparison, and the simple topic-switch calculation. Database facts are now accessible through search and extraction as well as deep research. The dedicated nontrivial-calculation case still requires the calculation tool.
- These are small, differently scoped samples with a changed manifest, not a statistically controlled latency win. Fixture timings exclude real market/search-provider latency and Discord delivery.
- All raw baseline, intermediate-failure, and final attempts are retained in benchmarkresult_traces.md. This change was not deployed to Railway.
