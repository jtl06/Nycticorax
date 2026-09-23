# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:42:09.267387+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `206e23c111ee405e8c63c84b2d624bcb`
- Manifest version: `22`
- Mode: `fixtures`
- Started: `2026-09-22T03:41:40.225266+00:00`
- Runtime: `29.0s`
- Pass rate: `3/4` (75.0%)
- Check score: `59/60` (98.3%)
- End-to-end latency: avg `7261ms`, p50 `3984ms`, p90 `14093ms`, max `14093ms`
- Agent averages: reply `7260ms`, turns `2`, tools `1.5`, tokens `7438`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,157 | final_text | - | 4.0s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,128 | final_text | - | 3.5s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 9,084 | final_text | - | 7.4s |
| `fixture-overnight-watchlist` | 1 | FAIL | 17/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,382 | final_text | - | 14.1s |

## Failures and errors

- `fixture-overnight-watchlist` attempt 1: answer:matches:2: required pattern '\\b(?:SPY|QQQ|SOXX)\\b' was missing
