# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:42:37.957063+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `f6ddb28fbd13412ea583c61fb923a9fe`
- Manifest version: `22`
- Mode: `fixtures`
- Started: `2026-09-22T03:42:10.416867+00:00`
- Runtime: `27.5s`
- Pass rate: `4/4` (100.0%)
- Check score: `60/60` (100.0%)
- End-to-end latency: avg `6885ms`, p50 `4232ms`, p90 `12889ms`, max `12889ms`
- Agent averages: reply `6885ms`, turns `2`, tools `1.5`, tokens `7384`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-overnight-session` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,135 | final_text | - | 4.2s |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 6,139 | final_text | - | 3.8s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,940 | final_text | - | 6.6s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,320 | final_text | - | 12.9s |
