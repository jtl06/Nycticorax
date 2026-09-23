# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:53:16.691177+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `e41efdfaef894f4f8ea0168076fb7a15`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:52:53.976288+00:00`
- Runtime: `22.7s`
- Pass rate: `3/4` (75.0%)
- Check score: `58/60` (96.7%)
- End-to-end latency: avg `5678ms`, p50 `3580ms`, p90 `11492ms`, max `11492ms`
- Agent averages: reply `5678ms`, turns `2`, tools `1.5`, tokens `7097`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,080 | final_text | - | 4.2s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist, quote | 2 | 6,085 | final_text | - | 3.5s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote | 2 | 7,931 | final_text | - | 3.6s |
| `fixture-overnight-watchlist` | 1 | FAIL | 16/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,291 | final_text | - | 11.5s |

## Failures and errors

- `fixture-overnight-watchlist` attempt 1: answer:matches:2: required pattern '\\b(?:SPY|QQQ|SOXX)\\b' was missing; answer:forbidden:1: forbidden pattern '\\bGTS\\b' was found
