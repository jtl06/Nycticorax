# Benchmark Results

Run: 2026-07-11T00:00:47Z (2026-07-10 evening America/Chicago)

Batch: `79f38bdbb5684b16b545543eb801d858`
Manifest: `2`
Mode: `all`
Runtime: `181.5s`

The run used Nycti's normal live-suite path with an isolated temporary SQLite database.
It did not post to Discord or change production data. Foreground inference was
`gpt-5.6-terra` through the official OpenAI endpoint with high reasoning. No call used
the DeepInfra fallback.

## Summary

- 28 attempts: 19 pass, 9 fail, 0 error, 0 skip
- Fixture cases: 12/20 pass
- Live canaries: 7/8 pass
- Total reported model tokens: 253,223
- The longest case was `canary-semis-sector` at 41.4s; it hit the deadline after
  quote, web, and deep-research work.

| Case | Status | Score | Tools | Turns | Tokens | Stop | Runtime |
| --- | --- | ---: | --- | ---: | ---: | --- | ---: |
| fixture-quick-recursion | PASS | 10/10 | - | 1 | 2,970 | final_text | 3.1s |
| fixture-calculation | FAIL | 8/9 | python | 2 | 5,917 | final_text | 2.3s |
| fixture-earnings-comparison | FAIL | 19/23 | deep_research, url_extract, web | 4 | 17,491 | final_text | 16.2s |
| fixture-fresh-release | PASS | 10/10 | web | 2 | 6,134 | final_text | 3.0s |
| fixture-fresh-news | PASS | 11/11 | web | 2 | 6,210 | final_text | 2.6s |
| fixture-opaque-version | FAIL | 9/11 | web | 2 | 6,167 | final_text | 3.4s |
| fixture-url-policy | PASS | 10/10 | url_extract | 2 | 6,065 | final_text | 3.1s |
| fixture-browser-dashboard | PASS | 11/11 | browser_extract, url_extract | 3 | 9,303 | final_text | 4.5s |
| fixture-market-quote | FAIL | 7/10 | web | 2 | 5,992 | final_text | 2.5s |
| fixture-price-history | PASS | 12/12 | price_hist | 2 | 6,211 | final_text | 2.9s |
| fixture-annual-performance | FAIL | 13/14 | annual_perf, price_hist, quote | 3 | 10,018 | final_text | 6.6s |
| fixture-transcript | PASS | 12/12 | yt_transcript | 2 | 6,139 | final_text | 2.8s |
| fixture-image-search | PASS | 10/10 | img_search | 2 | 6,008 | final_text | 3.0s |
| fixture-memory-private | PASS | 13/13 | memory_search | 2 | 6,068 | final_text | 2.2s |
| fixture-memory-shared | PASS | 12/12 | memory_search | 2 | 6,028 | final_text | 1.9s |
| fixture-memory-lore | PASS | 13/13 | memory_search | 2 | 6,095 | final_text | 2.1s |
| fixture-channel-decision | FAIL | 22/23 | channel_ctx | 2 | 6,567 | final_text | 2.7s |
| fixture-deep-comparison | FAIL | 14/15 | deep_research, web | 4 | 15,531 | final_text | 13.2s |
| fixture-composite-mixed | FAIL | 9/18 | deep_research, python, url_extract, web, yt_transcript | 3 | 10,296 | final_text | 5.9s |
| fixture-honest-missing-url | PASS | 10/10 | url_extract | 2 | 5,950 | final_text | 2.2s |
| canary-openai-latest | PASS | 16/16 | web | 2 | 7,390 | final_text | 6.4s |
| canary-openai-news | PASS | 14/14 | web | 2 | 6,480 | final_text | 5.7s |
| canary-spy-quote | PASS | 14/14 | quote | 2 | 5,771 | final_text | 3.4s |
| canary-spacex-price | PASS | 16/16 | quote, url_extract, web | 4 | 17,490 | final_text | 8.7s |
| canary-semis-sector | FAIL | 13/17 | deep_research, quote, web | 3 | 26,370 | deadline | 41.4s |
| canary-example-url | PASS | 12/12 | url_extract | 2 | 5,647 | final_text | 2.2s |
| canary-image-search | PASS | 12/12 | img_search | 2 | 6,210 | final_text | 4.3s |
| canary-deep-openai | PASS | 18/18 | deep_research | 3 | 26,705 | final_text | 22.9s |

## Failure Themes

- Formatting-only scorer misses: calculation emitted `568,826,903`; earnings emitted
  correct compact `B` and `M` values; Pyra used `lease-based`/`mutex-based` phrasing.
- Tool-choice/control-loop misses: the market-quote fixture called rejected web search
  instead of `quote`; composite research sent mismatched inputs to `deep_research` then
  fanned out into five calls; annual performance made three unnecessary follow-up calls.
- Context synthesis: the channel-decision answer correctly found the final plan but
  repeated a superseded blue-green proposal.
- Efficiency: deep comparison used one extra turn; semis exceeded its latency and
  token caps, then finalized after a transient post-tool call failure.

Detailed answers, tool outcomes, and agent traces for all failures are in
[`benchmarkresult_traces.md`](benchmarkresult_traces.md).

## Verification

The focused benchmark and provider tests passed: 94 tests.
