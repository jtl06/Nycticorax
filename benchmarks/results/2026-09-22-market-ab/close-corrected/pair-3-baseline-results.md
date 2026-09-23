# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:47:16.711844+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `2d4c2de2390d4bca80bd295f4733cfb5`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:47:12.517697+00:00`
- Runtime: `4.2s`
- Pass rate: `1/1` (100.0%)
- Check score: `12/12` (100.0%)
- End-to-end latency: avg `4194ms`, p50 `4194ms`, p90 `4194ms`, max `4194ms`
- Agent averages: reply `4194ms`, turns `2`, tools `1`, tokens `6014`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist | 2 | 6,014 | final_text | - | 4.2s |
