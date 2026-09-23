# Benchmark Results

Revision: `0450e3f + working tree`
Captured: `2026-09-22T03:46:45.786356+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `c71de8377b3d43038e344b3b94f663ce`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:46:40.036895+00:00`
- Runtime: `5.7s`
- Pass rate: `1/1` (100.0%)
- Check score: `12/12` (100.0%)
- End-to-end latency: avg `5749ms`, p50 `5749ms`, p90 `5749ms`, max `5749ms`
- Agent averages: reply `5749ms`, turns `2`, tools `2`, tokens `6214`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist, quote | 2 | 6,214 | final_text | - | 5.7s |
