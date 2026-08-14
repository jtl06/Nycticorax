# Benchmark Results

Revision: `1487092 + working tree`
Captured: `2026-08-14T18:59:35.031030+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `b26ee53100a842d2ab70bec38e842d35`
- Manifest version: `15`
- Mode: `fixtures`
- Started: `2026-08-14T18:59:26.697654+00:00`
- Runtime: `8.3s`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote | 2 | 8,951 | final_text | - | 4.9s |
| `fixture-terse-stock-callback` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 8,454 | final_text | - | 3.4s |
