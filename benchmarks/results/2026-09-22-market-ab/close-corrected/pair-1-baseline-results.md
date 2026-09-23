# Benchmark Results

Revision: `0450e3f`
Captured: `2026-09-22T03:46:38.957606+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `fae3c4810e4041b89425d5aeaae4601c`
- Manifest version: `23`
- Mode: `fixtures`
- Started: `2026-09-22T03:46:33.571230+00:00`
- Runtime: `5.4s`
- Pass rate: `1/1` (100.0%)
- Check score: `12/12` (100.0%)
- End-to-end latency: avg `5386ms`, p50 `5386ms`, p90 `5386ms`, max `5386ms`
- Agent averages: reply `5386ms`, turns `2`, tools `1`, tokens `6009`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-market-explicit-close` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | price_hist | 2 | 6,009 | final_text | - | 5.4s |
