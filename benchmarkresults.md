# Benchmark Results

Revision: `a411586 + working tree`
Captured: `2026-08-10T03:51:45.493195+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `de7a06828e724d2a86d06ba4482d7540`
- Manifest version: `13`
- Mode: `all`
- Started: `2026-08-10T03:47:58.900653+00:00`
- Runtime: `226.6s`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-quick-recursion` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,713 | final_text | - | 3.1s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,693 | final_text | - | 1.3s |
| `fixture-calculation` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | calc | 2 | 7,525 | final_text | - | 2.7s |
| `fixture-earnings-comparison` | 1 | PASS | 23/23 | gpt-5.6-terra | openai | deep_research | 2 | 8,776 | final_text | - | 6.2s |
| `fixture-fresh-release` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | web | 2 | 7,723 | final_text | - | 2.8s |
| `fixture-fresh-news` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 7,701 | final_text | - | 3.8s |
| `fixture-opaque-version` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 7,719 | final_text | - | 3.1s |
| `fixture-url-policy` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 7,655 | final_text | - | 2.7s |
| `fixture-browser-dashboard` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | browser_extract, url_extract | 3 | 11,686 | final_text | - | 6.2s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote, web | 3 | 11,732 | final_text | - | 4.7s |
| `fixture-full-market-scope` | 1 | FAIL | 13/14 | gpt-5.6-terra | openai | quote, web | 2 | 9,207 | final_text | - | 12.0s |
| `fixture-overnight-watchlist` | 1 | FAIL | 15/16 | gpt-5.6-terra | openai | channel_ctx, quote, web | 3 | 15,376 | final_text | - | 14.1s |
| `fixture-price-history` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | price_hist | 2 | 7,775 | final_text | - | 2.7s |
| `fixture-annual-performance` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | annual_perf | 2 | 7,821 | final_text | - | 3.0s |
| `fixture-transcript` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | yt_transcript | 2 | 7,709 | final_text | - | 3.0s |
| `fixture-image-search` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | img_search | 2 | 7,609 | final_text | - | 3.8s |
| `fixture-memory-private` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 7,692 | final_text | - | 2.6s |
| `fixture-memory-shared` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | memory_search | 2 | 7,686 | final_text | - | 3.0s |
| `fixture-memory-lore` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 7,712 | final_text | - | 2.8s |
| `fixture-memory-prefetch` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | - | 1 | 3,962 | final_text | - | 1.3s |
| `fixture-memory-named-shared-watchlist` | 1 | FAIL | 11/14 | gpt-5.6-terra | openai | memory_search | 3 | 10,722 | final_text | - | 5.1s |
| `fixture-memory-temporal` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 3,898 | final_text | - | 1.1s |
| `fixture-channel-decision` | 1 | PASS | 24/24 | gpt-5.6-terra | openai | channel_ctx | 2 | 8,126 | final_text | - | 3.0s |
| `fixture-deep-comparison` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | deep_research | 2 | 9,046 | final_text | - | 9.4s |
| `fixture-composite-mixed` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | deep_research | 2 | 8,646 | final_text | - | 5.2s |
| `fixture-honest-missing-url` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 7,637 | final_text | - | 4.7s |
| `canary-openai-latest` | 1 | PASS | 17/17 | gpt-5.6-terra | openai | web | 2 | 7,516 | final_text | - | 5.6s |
| `canary-openai-news` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | web | 2 | 7,906 | final_text | - | 8.1s |
| `canary-spy-quote` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | quote | 2 | 7,307 | final_text | - | 3.4s |
| `canary-spacex-price` | 1 | FAIL | 15/17 | gpt-5.6-terra | openai | quote, web | 5 | 22,883 | final_text | - | 12.6s |
| `canary-semis-sector` | 1 | ERROR | 0/0 | gpt-5.6-terra | openai | quote, web | 3 | 22,898 | deadline | - | 45.0s |
| `canary-example-url` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | url_extract | 2 | 7,123 | final_text | - | 2.8s |
| `canary-vision-ocr` | 1 | FAIL | 20/26 | gpt-5.6-terra | openai | - | 1 | 3,963 | final_text | - | 6.1s |
| `canary-image-search` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | img_search | 2 | 7,739 | final_text | - | 4.2s |
| `canary-deep-openai` | 1 | FAIL | 17/18 | gpt-5.6-terra | openai | deep_research | 2 | 17,696 | final_text | - | 25.3s |

## Failures and errors

- `fixture-full-market-scope` attempt 1: answer:matches:3: required pattern '\\bSOXX\\b' was missing
- `fixture-overnight-watchlist` attempt 1: answer:matches:1: required pattern '\\bNVDA\\b' was missing
- `fixture-memory-named-shared-watchlist` attempt 1: tool:not_called:memory_search: memory_search was called; metric:max:agent_model_turn_count: observed 3; required at most 1; tool:max_calls: tool call count was 1; limit is 0
- `canary-spacex-price` attempt 1: tool:succeeded:quote: quote did not succeed; metric:max:agent_model_turn_count: observed 5; required at most 4
- `canary-semis-sector` attempt 1: agent run ended in infrastructure fallback: timeout
- `canary-vision-ocr` attempt 1: answer:matches:3: required pattern '(?<!\\d)0?6\\D{1,24}11\\D{1,24}31\\D{1,24}33\\D{1,24}53\\D{1,24}18(?!\\d)' was missing; answer:matches:5: required pattern '(?<!\\d)34\\D{1,24}47\\D{1,24}49\\D{1,24}60\\D{1,24}65\\D{1,24}20(?!\\d)' was missing; answer:matches:7: required pattern '(?<!\\d)11\\D{1,24}31\\D{1,24}35\\D{1,24}53\\D{1,24}62\\D{1,24}20(?!\\d)' was missing; answer:matches:8: required pattern '(?<!\\d)17\\D{1,24}22\\D{1,24}52\\D{1,24}57\\D{1,24}63\\D{1,24}12(?!\\d)' was missing; answer:matches:9: required pattern '(?<!\\d)22\\D{1,24}38\\D{1,24}41\\D{1,24}47\\D{1,24}61\\D{1,24}0?3(?!\\d)' was missing; answer:matches:12: required pattern '(?<!\\d)0?5\\D{1,24}19\\D{1,24}35\\D{1,24}42\\D{1,24}66\\D{1,24}25(?!\\d)' was missing
- `canary-deep-openai` attempt 1: answer:forbidden:1: forbidden pattern "\\b(?:knowledge cutoff|can't browse|cannot browse|no internet access)\\b" was found

## Run Analysis

- The new social-banter case passed in one model turn with no tools.
- The new overnight-watchlist case reproduced the production failure: Nycti correctly omitted the Discord-member
  alias `GTS`, but its quote batch and final answer still omitted `NVDA`.
- The new full-market case covered `SPY`, `QQQ`, `IWM`, and semiconductor leadership; its lone failure was the stricter
  requirement to print `SOXX`, so this is a benchmark-calibration issue rather than a narrow single-stock response.
- The frozen market fixture was dated August 9 while the fixture clock remained July 10, causing both new market cases
  to spend output on a date-conflict caveat. Align those dates before the next canonical run.
- Existing regressions remain visible: the semiconductor canary hit the 45-second deadline, SpaceX failed to resolve a
  successful quote, vision OCR misread 6 of 15 rows, and named-watchlist recall used an unnecessary memory-search turn.
- The deep-OpenAI canary's only failure was a false positive: it mentioned a model's factual knowledge-cutoff date,
  while the forbidden regex currently treats any `knowledge cutoff` phrase as a browsing refusal.
