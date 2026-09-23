# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:41:38.888568+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `900c0365b79846dcbe047e10e8563c52`
- Manifest version: `22`
- Mode: `fixtures`
- Started: `2026-09-22T03:41:00.154395+00:00`
- Runtime: `38.7s`
- Pass rate: `2/4` (50.0%)
- Check score: `57/60` (95.0%)
- End-to-end latency: avg `9684ms`, p50 `7002ms`, p90 `17667ms`, max `17667ms`
- Agent averages: reply `9683ms`, turns `2`, tools `1.5`, tokens `7491`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | FAIL | 11/12 | gpt-5.6-terra | openai | quote | 2 | 6,095 | final_text | - | 7.0s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,077 | final_text | - | 4.3s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 9,102 | final_text | - | 9.7s |
| `fixture-overnight-watchlist` | 1 | FAIL | 16/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,690 | final_text | - | 17.7s |

## Failures and errors

- `fixture-market-overnight-session` attempt 1: answer:matches:2: required pattern '(?:[+]2(?:[.]0+)?\\s*%|(?:up|gained|rose)\\s+2(?:[.]0+)?\\s*%)' was missing
- `fixture-overnight-watchlist` attempt 1: answer:matches:2: required pattern '\\b(?:SPY|QQQ|SOXX)\\b' was missing; answer:forbidden:1: forbidden pattern '\\bGTS\\b' was found
