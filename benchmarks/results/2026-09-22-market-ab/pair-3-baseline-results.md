# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:43:42.606120+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `37e399c3986e479f8b32d51c1f235f6b`
- Manifest version: `22`
- Mode: `fixtures`
- Started: `2026-09-22T03:43:11.176946+00:00`
- Runtime: `31.4s`
- Pass rate: `3/4` (75.0%)
- Check score: `57/60` (95.0%)
- End-to-end latency: avg `7858ms`, p50 `6017ms`, p90 `13296ms`, max `13296ms`
- Agent averages: reply `7857ms`, turns `2`, tools `1.5`, tokens `7356`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,100 | final_text | - | 4.5s |
| `fixture-market-explicit-close` | 1 | FAIL | 9/12 | gpt-5.6-terra | openai | price_hist | 2 | 5,939 | final_text | - | 6.0s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,971 | final_text | - | 7.6s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,416 | final_text | - | 13.3s |

## Failures and errors

- `fixture-market-explicit-close` attempt 1: answer:matches:1: required pattern '\\b100(?:[.]00)?\\b' was missing; answer:matches:2: required pattern '5[.]26\\s*%' was missing; tool:succeeded:quote: quote did not succeed
