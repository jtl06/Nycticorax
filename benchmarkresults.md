# Benchmark Results

Commit: `6abc5fb Improve benchmark routing and evidence delivery`
Manifest: `3`
Run window: `2026-07-11T01:38:06.260283+00:00` to `2026-07-11T01:41:49.634077+00:00`

The canonical 28-case suite was executed as four sequential isolated SQLite batches. Fixture cases used
frozen tool results; canaries used the configured live providers. No Discord messages or production database
records were created.

## Summary

- 28 attempts: 23 pass, 5 fail, 0 error, 0 skip
- Fixture cases: 17/20 pass
- Live canaries: 6/8 pass
- Combined batch runtime: `179.6s`
- Reported model tokens: `288,219`
- Providers: `openai`; models: `gpt-5.6-terra`

| Case | Status | Score | Tools | Turns | Tokens | Stop | Runtime |
| --- | --- | ---: | --- | ---: | ---: | --- | ---: |
| `fixture-quick-recursion` | PASS | 10/10 | - | 1 | 3446 | final_text | 2.4s |
| `fixture-calculation` | PASS | 9/9 | python | 2 | 6951 | final_text | 2.9s |
| `fixture-earnings-comparison` | PASS | 23/23 | deep_research | 2 | 8300 | final_text | 5.5s |
| `fixture-fresh-release` | PASS | 10/10 | web | 2 | 7145 | final_text | 3.5s |
| `fixture-fresh-news` | PASS | 11/11 | web | 2 | 7161 | final_text | 3.4s |
| `fixture-opaque-version` | FAIL | 10/11 | memory_search, web | 3 | 8943 | model_turn_budget | 4.3s |
| `fixture-url-policy` | PASS | 10/10 | url_extract | 2 | 7055 | final_text | 2.9s |
| `fixture-browser-dashboard` | PASS | 11/11 | browser_extract, url_extract | 3 | 10698 | final_text | 4.2s |
| `fixture-market-quote` | PASS | 10/10 | quote | 2 | 7035 | final_text | 2.6s |
| `fixture-price-history` | PASS | 12/12 | price_hist | 2 | 7171 | final_text | 2.9s |
| `fixture-annual-performance` | PASS | 14/14 | annual_perf | 2 | 7210 | final_text | 3.2s |
| `fixture-transcript` | PASS | 12/12 | yt_transcript | 2 | 7121 | final_text | 2.8s |
| `fixture-image-search` | PASS | 10/10 | img_search | 2 | 7017 | final_text | 3.0s |
| `fixture-memory-private` | PASS | 13/13 | memory_search | 2 | 7049 | final_text | 2.7s |
| `fixture-memory-shared` | PASS | 12/12 | memory_search | 2 | 7035 | final_text | 2.5s |
| `fixture-memory-lore` | PASS | 13/13 | memory_search | 2 | 7067 | final_text | 2.9s |
| `fixture-channel-decision` | PASS | 23/23 | channel_ctx | 2 | 7532 | final_text | 3.1s |
| `fixture-deep-comparison` | PASS | 15/15 | deep_research | 2 | 8227 | final_text | 8.3s |
| `fixture-composite-mixed` | FAIL | 17/18 | deep_research | 3 | 12526 | final_text | 9.2s |
| `fixture-honest-missing-url` | FAIL | 9/10 | url_extract | 2 | 6892 | final_text | 2.7s |
| `canary-openai-latest` | PASS | 16/16 | web | 2 | 7274 | final_text | 4.6s |
| `canary-openai-news` | PASS | 14/14 | web | 2 | 7363 | final_text | 5.1s |
| `canary-spy-quote` | PASS | 14/14 | quote | 2 | 6755 | final_text | 4.5s |
| `canary-spacex-price` | FAIL | 14/16 | quote, web | 5 | 27154 | final_text | 16.1s |
| `canary-semis-sector` | PASS | 15/15 | python, quote, web | 4 | 24269 | final_text | 28.7s |
| `canary-example-url` | PASS | 12/12 | url_extract | 2 | 6625 | final_text | 3.0s |
| `canary-image-search` | PASS | 12/12 | img_search | 2 | 7149 | final_text | 5.1s |
| `canary-deep-openai` | FAIL | 15/18 | deep_research, url_extract, web | 5 | 50049 | final_text | 37.4s |

## Raw Failure Traces

Exact failed checks, answers, agent traces, and step records are in
[`benchmarkresult_traces.md`](benchmarkresult_traces.md).

## Retry Samples

After the canary batch’s terminal progress stream returned early, five individual retry samples were run.
They are not included in the canonical 28-case score above; their raw outputs are included in the trace file.

| Case | Status | Score | Runtime |
| --- | --- | ---: | ---: |
| `canary-spacex-price` | PASS | 16/16 | 14.1s |
| `canary-semis-sector` | PASS | 15/15 | 24.1s |
| `canary-example-url` | PASS | 12/12 | 2.8s |
| `canary-image-search` | FAIL | 11/12 | 5.8s |
| `canary-deep-openai` | PASS | 18/18 | 19.5s |
