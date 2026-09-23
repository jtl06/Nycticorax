# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:43:09.945222+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `625239678e5b4e61938b4cdc13dabd1f`
- Manifest version: `22`
- Mode: `fixtures`
- Started: `2026-09-22T03:42:39.196771+00:00`
- Runtime: `30.7s`
- Pass rate: `3/4` (75.0%)
- Check score: `59/60` (98.3%)
- End-to-end latency: avg `7687ms`, p50 `6635ms`, p90 `11012ms`, max `11012ms`
- Agent averages: reply `7687ms`, turns `2`, tools `1.75`, tokens `7434`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,017 | final_text | - | 3.9s |
| `fixture-market-explicit-close` | 1 | FAIL | 11/12 | gpt-5.6-terra | openai | price_hist, quote | 2 | 6,440 | final_text | - | 6.6s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,976 | final_text | - | 9.2s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,303 | final_text | - | 11.0s |

## Failures and errors

- `fixture-market-explicit-close` attempt 1: tool:max_calls: tool call count was 2; limit is 1
