# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:44:12.737575+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `932a7887e01f41fd9578323de5906de2`
- Manifest version: `22`
- Mode: `fixtures`
- Started: `2026-09-22T03:43:43.879962+00:00`
- Runtime: `28.9s`
- Pass rate: `2/4` (50.0%)
- Check score: `56/60` (93.3%)
- End-to-end latency: avg `7214ms`, p50 `5699ms`, p90 `13406ms`, max `13406ms`
- Agent averages: reply `7214ms`, turns `2`, tools `1.5`, tokens `7162`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | FAIL | 11/12 | gpt-5.6-terra | openai | quote | 2 | 6,108 | final_text | - | 5.7s |
| `fixture-market-explicit-close` | 1 | FAIL | 9/12 | gpt-5.6-terra | openai | price_hist | 2 | 5,820 | final_text | - | 3.7s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,095 | final_text | - | 6.1s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,627 | final_text | - | 13.4s |

## Failures and errors

- `fixture-market-overnight-session` attempt 1: answer:matches:2: required pattern '(?:[+]2(?:[.]0+)?\\s*%|(?:up|gained|rose)\\s+2(?:[.]0+)?\\s*%)' was missing
- `fixture-market-explicit-close` attempt 1: answer:matches:1: required pattern '\\b100(?:[.]00)?\\b' was missing; answer:matches:2: required pattern '5[.]26\\s*%' was missing; tool:succeeded:quote: quote did not succeed
