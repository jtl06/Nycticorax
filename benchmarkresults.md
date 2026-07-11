# Benchmark Results

Commit: `0c51f20 Improve response progress and benchmark reliability`
Manifest: `4`
Batch: `72c37ee768df4c4e87f9eb6f2f0e7cc3`
Captured: `2026-07-11T05:10:48.430774+00:00`

The suite used the normal isolated live-benchmark executor with temporary SQLite state. Fixture cases used
frozen tool results; canaries used configured live providers. It did not post to Discord or write production data.

## Summary

- 28 attempts: 24 pass, 3 fail, 1 error, 0 skip
- Fixture cases: 18/20 pass
- Live canaries: 6/8 pass
- Runtime: `198.8s`
- Reported model tokens: `314,174`
- Providers: `api.deepinfra.com, openai`; models: `deepseek-ai/DeepSeek-V4-Pro, gpt-5.6-terra`

| Case | Status | Score | Tools | Turns | Tokens | Stop | Runtime |
| --- | --- | ---: | --- | ---: | ---: | --- | ---: |
| `fixture-quick-recursion` | PASS | 10/10 | - | 1 | 3645 | final_text | 2.8s |
| `fixture-calculation` | PASS | 9/9 | python | 2 | 7311 | final_text | 2.4s |
| `fixture-earnings-comparison` | PASS | 23/23 | deep_research | 2 | 8743 | final_text | 5.4s |
| `fixture-fresh-release` | PASS | 10/10 | web | 2 | 7497 | final_text | 2.9s |
| `fixture-fresh-news` | PASS | 11/11 | web | 2 | 7560 | final_text | 3.9s |
| `fixture-opaque-version` | PASS | 11/11 | web | 2 | 7487 | final_text | 2.6s |
| `fixture-url-policy` | PASS | 10/10 | url_extract | 2 | 7439 | final_text | 1.9s |
| `fixture-browser-dashboard` | PASS | 11/11 | browser_extract, url_extract | 3 | 11326 | final_text | 6.3s |
| `fixture-market-quote` | PASS | 10/10 | quote, web | 3 | 11373 | final_text | 3.7s |
| `fixture-price-history` | PASS | 12/12 | price_hist | 2 | 7611 | final_text | 3.0s |
| `fixture-annual-performance` | PASS | 14/14 | annual_perf | 3 | 13140 | final_text | 18.3s |
| `fixture-transcript` | PASS | 12/12 | yt_transcript | 3 | 12977 | final_text | 5.8s |
| `fixture-image-search` | PASS | 10/10 | img_search | 2 | 8339 | final_text | 1.8s |
| `fixture-memory-private` | PASS | 13/13 | memory_search | 2 | 8415 | final_text | 1.5s |
| `fixture-memory-shared` | PASS | 12/12 | memory_search | 2 | 8419 | final_text | 1.5s |
| `fixture-memory-lore` | PASS | 13/13 | memory_search | 2 | 8452 | final_text | 1.5s |
| `fixture-channel-decision` | PASS | 23/23 | channel_ctx | 2 | 8897 | final_text | 3.0s |
| `fixture-deep-comparison` | FAIL | 14/15 | deep_research, web | 4 | 19814 | final_text | 12.2s |
| `fixture-composite-mixed` | PASS | 18/18 | deep_research | 2 | 9379 | final_text | 4.6s |
| `fixture-honest-missing-url` | FAIL | 9/10 | url_extract | 2 | 8314 | final_text | 3.7s |
| `canary-openai-latest` | PASS | 16/16 | web | 3 | 15628 | final_text | 6.6s |
| `canary-openai-news` | PASS | 14/14 | web | 2 | 9205 | final_text | 11.6s |
| `canary-spy-quote` | PASS | 14/14 | quote | 2 | 7921 | final_text | 3.6s |
| `canary-spacex-price` | FAIL | 14/16 | quote, url_extract, web | 5 | 25134 | final_text | 10.1s |
| `canary-semis-sector` | ERROR | 0/0 | python, quote, web | 4 | 28828 | deadline | 45.0s |
| `canary-example-url` | PASS | 12/12 | url_extract | 2 | 7045 | final_text | 2.4s |
| `canary-image-search` | PASS | 12/12 | img_search | 2 | 7460 | final_text | 4.0s |
| `canary-deep-openai` | PASS | 18/18 | deep_research, url_extract | 3 | 26815 | final_text | 26.7s |

## Raw Traces

The current failed/error outputs, agent traces, and serialized step records are in
[`benchmarkresult_traces.md`](benchmarkresult_traces.md).
