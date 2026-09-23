# Benchmark Results

## Market Grounding A/B - 2026-09-22

**Normal-chat comparison: candidate 11/12 passes, baseline 10/12.** Both scored 178/180
individual checks. The repository regression comparator passes; this is not a clean suite pass
or proof of a general quality improvement.

Terra High, standard tier, 8192-token output budget, frozen market tools and temporary SQLite.
Three alternating A/B pairs per case, 24 evaluated attempts. Baseline `0450e3f` versus
`codex/grounding-latency-memory` working tree. Same prompts, fixtures and settings on both sides.
Cross-provider fallback was disabled to isolate Terra and bound cost. No Discord posts or production writes.
The wrapper uses normal production `EvidenceMode.INTERNAL` on both arms; synthetic context remains isolated.

| Case | Baseline pass | Candidate pass | Baseline median | Candidate median | Median paired delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| `fixture-active-watchlist` | 3/3 | 3/3 | 5.44s | 4.19s | -1.21s |
| `fixture-market-explicit-close` | 3/3 | 3/3 | 4.33s | 3.48s | -0.59s |
| `fixture-market-overnight-session` | 3/3 | 3/3 | 4.55s | 3.90s | -0.60s |
| `fixture-overnight-watchlist` | 1/3 | 2/3 | 13.90s | 11.49s | -2.40s |

Average isolated response latency: **7.015s -> 6.267s** (10.7% lower in this sample).
These are model-loop timings with frozen tools, not live tool/Discord delivery e2e timings.
Three samples per case cannot establish a stable speedup or tail-latency improvement; cache and provider
variance remain confounders. Neither the 30-second failover threshold nor Railway idle RSS was exercised here.

### Remaining Failure

The candidate's third overnight-watchlist answer omitted SPY/QQQ/SOXX and mentioned the member alias GTS as
an unverified ticker. Baseline instead made unnecessary channel/memory lookups in two attempts. Simple
overnight and historical-close prices were correct in all normal-chat samples on both arms. This does not
reproduce the original generic evening-report failure with production memory; the newer-session tool
presentation change is covered offline, not by these frozen-provider fixtures.

### Test-Harness Findings

The initial run had two test defects: the percentage regex rejected a correct `up $2.00 / 2.0%` answer,
and the closing fixture blocked legitimate `price_hist` requests. Original v22 outputs/scores are retained.
The regex fix was applied equally during offline rescoring; six invalid close attempts were excluded and
replaced by six v23 shared-history reruns. No runtime prompt or model change was made between phases.

That corrected **citation-mode** comparison failed the strict gate (baseline 10/12, candidate 9/12):
correct closing answers sometimes needed a fourth turn to add citations. `bot.py` selects cited mode for
isolated benchmarks but internal evidence for normal Discord chats. A separate full normal-mode comparison
above was therefore run, with the unchanged v23 manifest and no post-hoc grading changes. Keep both results;
do not report only the favorable phase or interpret benchmark-only citation repairs as normal-chat latency.

### Cost and Artifacts

54 total attempts across all phases; 116 API requests. Conservative usage-accounted cost **$1.217492**, below
the user-approved $2 ceiling. Each request reserved UTF-8 input bytes plus framing headroom and full output
capacity before dispatch. Known usage settled at $2.50/M input (including potential cache-write premium;
no discount assumed) and $12/M output; unknown/failed requests would retain the full reserve. These are
upper accounting estimates from [official pricing](https://developers.openai.com/api/docs/pricing), not an invoice.

- [Normal-mode comparison and source hashes](benchmarks/results/2026-09-22-market-ab/normal-chat/comparison.json)
- [Citation-mode comparison](benchmarks/results/2026-09-22-market-ab/comparison.json)
- [Per-request spend ledger](benchmarks/results/2026-09-22-market-ab/spend.json)
- [Initial raw attempts, including excluded fixture failures](benchmarks/results/2026-09-22-market-ab/raw-attempts.json)
- [Latest normal-mode raw attempts](benchmarks/results/2026-09-22-market-ab/normal-chat/raw-attempts.json)
- `benchmarkresult_traces.md` contains the complete latest 24-attempt dump, not a paraphrase.

Offline verification: `scripts/check_ci.py` passed (Ruff, selected mypy, compileall, **1,068 tests**).
Production, model settings, worker count and memory state remain unchanged. No commit or deployment made.

## I/O Worker Component Experiment - 2026-09-21

Local macOS/Python 3.11 synthetic test, not a live model-suite rerun or Railway RSS test.
Baseline `0450e3f`, candidate `codex/grounding-latency-memory`; only worker capacity varied.
Three alternating pairs per workload, 12 fresh processes total. Each job parses the same
JSON and simulates 50 ms of blocking I/O. Every result matched; no external APIs or Discord posts.

| Concurrent reads | 32-worker median | 24-worker median | Idle RSS median, 32 / 24 |
| --- | ---: | ---: | ---: |
| 24 | 103.17 ms | 103.69 ms | 57.2 / 56.9 MiB |
| 48 (overlapping requests) | 147.27 ms | 155.82 ms | 80.8 / 70.2 MiB |

The overlap case retained roughly 10.6 MiB less but took roughly 5.8% longer in this
synthetic workload. **Do not change the production default from this result.** Platform,
allocator, payload and real I/O durations differ; no production savings or e2e improvement is claimed.
`IO_MAX_WORKERS` remains opt-in. Raw samples: [JSON](benchmarks/results/2026-09-21-io-workers.json)
and the full dump in `benchmarkresult_traces.md`. Two new frozen session-selection cases
were added but not called against models because the paid API budget is zero.

## Context Fetch A/B - 2026-09-15

Read-only Discord REST benchmark executed from Railway against the bot-test channel.
Baseline: `7fe6790`; candidate: `codex/context-batching` working tree.
Three alternating paired samples per case (18 total executions). No Discord posts,
database writes, gateway connection, or model calls. This measures context collection,
not complete answer generation or Discord end-to-end latency.

| Case | Baseline median | Candidate median | REST operations before / after |
| --- | ---: | ---: | --- |
| Recent reply, cold cache | 5159.47 ms | 181.31 ms | 6 / 2 |
| Recent reply, warm cache | 5031.60 ms | 0.83 ms | 4 / 0 |
| Older uncached anchor | 418.81 ms | 446.30 ms | 3 / 2 |

All nine pairs returned identical context text, image URLs and image labels. The first
two cases replay a real recent human reply; the older-anchor case is a synthetic reply
reference to an older real test-channel message, never posted to Discord.

The older-anchor median is 27.49 ms slower: waiting for recent history to enable reuse
can remove useful overlap when the anchor is old. Fewer requests are not an unconditional
latency improvement. Recent-reply results support this candidate; three samples do not
establish p90/p95 or general answer-quality improvements. SDK HTTP spans include network,
rate-limit waits and retries; counts are logical SDK request operations, not wire attempts.

Verification: 1,056 offline tests plus Ruff, configured mypy and compilation passed.
These are pre-deployment A/B measurements; they do not establish production E2E gains.
[Raw numeric traces](benchmarkresult_traces.md)
retain every sample, not just the fastest or failed ones. Complete private raw output is
under `.local/maintenance/context-batching-20260915/` in the main checkout; message text
and identifiers are excluded from committed artifacts.

## Previous Full Agent Benchmark


Revision: `e0cf3c4 + working tree`
Captured: `2026-09-04T23:48:13.327127+00:00`
Execution: isolated Nycti agent loop with temporary SQLite; fixture cases use frozen tools and canaries use configured live providers.

# Nycti Live LLM Benchmark

- Batch: `9a7b7babf9a74c8eb06992e249d51b8e`
- Manifest version: `21`
- Mode: `fixtures`
- Started: `2026-09-04T23:46:06.310108+00:00`
- Runtime: `127.0s`
- Pass rate: `37/37` (100.0%)
- Check score: `470/470` (100.0%)
- End-to-end latency: avg `3433ms`, p50 `2756ms`, p90 `6747ms`, max `15686ms`
- Agent averages: reply `3432ms`, turns `1.7`, tools `0.86`, tokens `5355`

| Case | Attempt | Status | Score | Model | Provider | Tools called | Turns | Tokens | Stop reason | Log ID | Runtime |
| --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |
| `fixture-quick-recursion` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 2,853 | final_text | - | 2.5s |
| `fixture-social-banter` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,779 | final_text | - | 1.7s |
| `fixture-calculation` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | calc | 2 | 5,735 | final_text | - | 2.7s |
| `fixture-earnings-comparison` | 1 | PASS | 23/23 | gpt-5.6-terra | openai | web | 2 | 7,150 | final_text | - | 5.9s |
| `fixture-fresh-release` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | web | 2 | 5,928 | final_text | - | 3.1s |
| `fixture-fresh-news` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 5,912 | final_text | - | 2.9s |
| `fixture-opaque-version` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | web | 2 | 6,028 | final_text | - | 3.8s |
| `fixture-url-policy` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 5,935 | final_text | - | 2.8s |
| `fixture-browser-dashboard` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | browser_extract, url_extract | 3 | 9,059 | final_text | - | 3.9s |
| `fixture-market-quote` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | quote | 2 | 5,924 | final_text | - | 3.6s |
| `fixture-active-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote | 2 | 7,910 | final_text | - | 3.7s |
| `fixture-terse-stock-callback` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 7,337 | final_text | - | 3.1s |
| `fixture-full-market-scope` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote, web | 2 | 7,166 | final_text | - | 10.8s |
| `fixture-overnight-watchlist` | 1 | PASS | 18/18 | gpt-5.6-terra | openai | quote, web | 2 | 8,015 | final_text | - | 7.2s |
| `fixture-price-history` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | price_hist | 2 | 6,027 | final_text | - | 3.7s |
| `fixture-annual-performance` | 1 | PASS | 15/15 | gpt-5.6-terra | openai | annual_perf | 2 | 5,999 | final_text | - | 3.5s |
| `fixture-transcript` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | yt_transcript | 2 | 6,022 | final_text | - | 3.0s |
| `fixture-image-search` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | img_search | 2 | 5,779 | final_text | - | 2.7s |
| `fixture-memory-private` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 5,853 | final_text | - | 4.0s |
| `fixture-memory-shared` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | memory_search | 2 | 5,846 | final_text | - | 2.5s |
| `fixture-memory-lore` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | memory_search | 2 | 5,863 | final_text | - | 3.9s |
| `fixture-memory-prefetch` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | - | 1 | 3,050 | final_text | - | 1.4s |
| `fixture-memory-named-shared-watchlist` | 1 | PASS | 14/14 | gpt-5.6-terra | openai | - | 1 | 3,082 | final_text | - | 1.3s |
| `fixture-memory-temporal` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 2,988 | final_text | - | 1.4s |
| `fixture-channel-decision` | 1 | PASS | 24/24 | gpt-5.6-terra | openai | channel_ctx | 2 | 6,296 | final_text | - | 3.2s |
| `fixture-deep-comparison` | 1 | PASS | 13/13 | gpt-5.6-terra | openai | web | 2 | 6,301 | final_text | - | 6.7s |
| `fixture-composite-mixed` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | calc, quote, url_extract, yt_transcript | 3 | 12,113 | final_text | - | 15.7s |
| `fixture-honest-missing-url` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | url_extract | 2 | 5,806 | final_text | - | 2.5s |
| `fixture-discord-reply-time` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,954 | final_text | - | 1.3s |
| `fixture-discord-correction` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 3,058 | final_text | - | 2.2s |
| `fixture-discord-summary` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | - | 1 | 2,946 | final_text | - | 1.5s |
| `fixture-discord-topic-switch` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,889 | final_text | - | 1.1s |
| `fixture-discord-banter-recovery` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 3,011 | final_text | - | 1.9s |
| `fixture-scenario-correction-1` | 1 | PASS | 10/10 | gpt-5.6-terra | openai | - | 1 | 2,776 | final_text | - | 1.2s |
| `fixture-scenario-correction-2` | 1 | PASS | 11/11 | gpt-5.6-terra | openai | - | 1 | 2,913 | final_text | - | 1.2s |
| `fixture-scenario-market-callback-1` | 1 | PASS | 12/12 | gpt-5.6-terra | openai | quote | 2 | 5,837 | final_text | - | 2.3s |
| `fixture-scenario-market-callback-2` | 1 | PASS | 9/9 | gpt-5.6-terra | openai | - | 1 | 2,988 | final_text | - | 1.0s |

## Simplification Validation

- The final full run used real OpenAI Terra inference with synthetic Discord context and frozen tool results; no production chat or memory was modified.
- OpenAI Terra and DeepInfra DeepSeek-V4-Pro-0813 each passed a separate native-tool call plus tool-result follow-up probe (about 2.12s and 1.70s respectively).
- The initial focused baseline passed 12/12, averaging 3618ms and 5580 tokens. The first simplified focused run passed 10/12, averaging 3753ms and 5119 tokens; both misses omitted the ticker from a mixed request. General ticker-shorthand guidance corrected those misses in a 2/2 recheck.
- The first full run passed 33/37. Two answers were numerically correct but missed tool-use checks; the database comparison hit a fixture that exposed its facts only through deep research; the overnight case incorrectly treated a member alias as a ticker.
- Manifest 21 keeps factual checks but removes route requirements from mixed research, database comparison, and the simple topic-switch calculation. Database facts are now accessible through search and extraction as well as deep research. The dedicated nontrivial-calculation case still requires the calculation tool.
- These are small, differently scoped samples with a changed manifest, not a statistically controlled latency win. Fixture timings exclude real market/search-provider latency and Discord delivery.
- All raw baseline, intermediate-failure, and final attempts are retained in benchmarkresult_traces.md. This change was not deployed to Railway.
