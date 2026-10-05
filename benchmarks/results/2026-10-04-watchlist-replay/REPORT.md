# Benchmark Results

## October 4, 2026: Watchlist Quote Recovery

Terra High passed 24/24 automated cases, with three samples per prompt. Five new cases cover
session-qualified full-basket quotes, scope preservation after a correction, an explicit list callback,
watchlist listing without unnecessary market calls, and an explicit inspirational quotation.
Three existing cases cover a single quote, an explicit historical close, and friend-server banter.

| Case | Passes | Median local pipeline time |
| --- | ---: | ---: |
| fixture-watchlist-session-quote | 3/3 | 4.02s |
| fixture-watchlist-market-correction | 3/3 | 3.95s |
| fixture-watchlist-list-callback | 3/3 | 3.95s |
| fixture-watchlist-list-only | 3/3 | 1.11s |
| fixture-watchlist-saying | 3/3 | 1.58s |
| fixture-market-explicit-close | 3/3 | 3.84s |
| fixture-social-banter | 3/3 | 1.92s |
| fixture-market-quote | 3/3 | 2.87s |

## Setup

- Base revision: `997fa4c`, plus the pending watchlist context/guidance patch; exact source hashes are in `experiment.json`.
- Model: `gpt-5.6-terra`, high reasoning, standard API tier, no SDK retries or provider fallback.
- Production's normal internal evidence mode; no benchmark-only instruction to use tools or force an answer repair.
- Real model calls through Nycti's isolated reply pipeline, temporary SQLite, synthetic Discord context, and frozen quote evidence.
- No production memories, Discord messages, or live market-provider requests. Timings exclude Discord delivery.
- User prompts are short: `24 hour quote`, `quote the market`, `Quote those then.`, `What's my watch list?`, and `Give me an inspirational quote.`

## Observations

The initial session quote and market correction included all ten canonical symbols in every repeat.
List callbacks stayed on the referenced three symbols instead of expanding to the default ten.
List-only and inspirational requests made no tool calls. Existing single-symbol, historical-session,
and banter cases passed. No provider or tool errors occurred in the recorded runs.

These checks validate task interpretation and quote coverage, not every incidental sentence.
Manual review found one reply incorrectly grouping SPCX with semiconductors and an unnecessary
timestamp caveat despite the fixture header. Those commentary issues remain visible in raw traces
and were not removed or silently rescored. There is no before/after live pass-rate comparison or
production latency claim from this replay.

## Cost and Artifacts

Conservative budget accounting: $0.3927 of the authorized $1 across 40 model requests.
Input is charged at the $2.50/M cache-write upper rate and output at $12/M, ignoring cache and sharing
discounts. Actual billed cost may be lower. Pricing checked October 4, 2026:
https://developers.openai.com/api/docs/pricing

- `benchmarks/results/2026-10-04-watchlist-replay/`: original logs, results, raw traces, JSON baselines, manifest, source hashes, and spend ledger.
- `benchmarkresult_traces.md`: latest complete raw attempt dump, without paraphrase.

The API replay supports the scope fix; offline lint, type checks and regression tests provide the code gate.
