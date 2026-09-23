# Market Grounding Comparison

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

