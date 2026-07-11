# Benchmark Result Traces

Run: `79f38bdbb5684b16b545543eb801d858`
Captured: 2026-07-11T00:00:47Z
Manifest: `2`
Mode: `all`

This is the sanitized failure-trace companion to
[`benchmarkresults.md`](benchmarkresults.md). It records outputs, model/tool paths,
and deterministic evaluation misses. It excludes API keys, raw provider payloads, and
Discord data.

## Shared Runtime

```text
primary provider: openai
primary model: gpt-5.6-terra
foreground reasoning: high (quick profile overrides to low)
fallback provider used: no
batch runtime: 181.5s
attempts: 28 total, 19 pass, 9 fail, 0 error
```

## Failure Traces

### fixture-calculation

```text
failed_check: answer:matches:1 required \b568826903\b
answer: 568,826,903

chat_turn: 1344ms model=gpt-5.6-terra tokens=2902 tool_calls=1
tool:python: 0ms status=ok
chat_turn: 935ms model=gpt-5.6-terra tokens=3015 tool_calls=0
```

The calculation and Python tool call were correct; the evaluator only accepts the
unformatted number.

### fixture-earnings-comparison

```text
failed_checks:
  - NVIDIA revenue/guidance compact formatting
  - AMD guidance compact formatting
  - turns=4, required at most 3

answer:
NVIDIA Q1 fiscal 2027: reported May 20, 2026; revenue $81.615B; adjusted diluted
EPS $1.87; Q2 guidance $91.0B revenue, +/-2%; official NVIDIA IR link.

AMD Q1 2026: reported May 5, 2026; revenue $10.253B; adjusted diluted EPS $1.37;
Q2 guidance about $11.2B revenue, +/-$300M; official AMD IR link.

trace:
  chat_turn: 2733ms tokens=3150 tool_calls=1
  tool:deep_research: 0ms status=ok
  chat_turn: 3605ms tokens=4092 tool_calls=2
  tool:url_extract NVIDIA IR: 0ms status=ok
  tool:url_extract AMD IR: 0ms status=ok
  chat_turn: 2787ms tokens=4615 tool_calls=1
  tool:web: 0ms status=ok
  chat_turn: 7022ms tokens=5634 tool_calls=0
```

The answer was factually complete. The practical issues are the redundant post-evidence
web call and scorer patterns that reject `B`/`M` suffixes.

### fixture-opaque-version

```text
failed_checks: required literal phrases "lease sessions" and "mutex sessions"
answer: Pyra 3.0 replaces 2.9's mutex-based sessions with lease-based sessions.

chat_turn: 1795ms tokens=2937 tool_calls=1
tool:web: 0ms status=ok
chat_turn: 1605ms tokens=3230 tool_calls=0
```

This is a morphology mismatch, not a factual one.

### fixture-market-quote

```text
failed_checks:
  - required price 137.25 missing
  - quote tool did not succeed
  - grounding score was 0
answer: I can't verify a current listing for "ACME" from that query.

chat_turn: 1440ms tokens=2963 tool_calls=1
tool:web: 0ms status=error (fixture rejected unrelated query)
chat_turn: 1105ms tokens=3029 tool_calls=0
```

This is a real routing miss: `quote` was directly available and should have been used.

### fixture-annual-performance

```text
failed_check: tool calls=4, maximum=2
answer: 2024 +12.5% with $1.20 distributions; 2025 +8.2% with $1.35 distributions.

chat_turn: 1630ms tokens=2960 tool_calls=1
tool:annual_perf: 0ms status=ok
chat_turn: 2955ms tokens=3390 tool_calls=3
tool:quote: 0ms status=error (fixture supports only ACME)
tool:price_hist: 0ms status=error (fixture supports only ACME)
tool:price_hist: 0ms status=error (fixture supports only ACME)
chat_turn: 2030ms tokens=3668 tool_calls=0
```

The first tool already supplied the complete answer. The remaining calls were waste.

### fixture-channel-decision

```text
failed_check: answer included forbidden superseded-plan wording
answer: Thursday June 18 at 16:00 UTC, 10% canary for 30 minutes, then full rollout;
Marcus owns rollback; Elena owns alerts; mobile refresh remains open; go/no-go is June 17
at 15:00 UTC. It also said this superseded the earlier blue-green proposal.

chat_turn: 1071ms tokens=2942 tool_calls=1
tool:channel_ctx: 0ms status=ok
chat_turn: 1670ms tokens=3625 tool_calls=0
```

The requested final facts were correct; the model unnecessarily preserved historical
context the benchmark explicitly asks it to omit.

### fixture-deep-comparison

```text
failed_check: turns=4, maximum=3
answer: AtlasDB supports 18,400 audited serializable TPS; NovaDB supports 11.8 TB/min
analytical scans. The evidence does not establish a general winner.

chat_turn: 2936ms tokens=3107 tool_calls=1
tool:deep_research: 0ms status=ok
chat_turn: 1583ms tokens=3673 tool_calls=1
tool:web: 0ms status=error (fixture rejected unrelated query)
chat_turn: 4026ms tokens=4019 tool_calls=0
chat_turn: 4624ms tokens=4732 tool_calls=0
```

The evidence was sufficient after `deep_research`; the web retry and extra completion
turn exceeded the bounded deep-case budget.

### fixture-composite-mixed

```text
failed_checks:
  - ACME price 137.25 missing
  - unformatted calculation pattern missing
  - deep_research did not succeed
  - all composite specialized-call metrics missing
  - tool calls=5, maximum=2

answer: 9173 x 62011 = 568,826,903; policy limit 37 requests/minute; migration is
inventory -> shadow traffic -> cutover; ACME was left unverified.

chat_turn: 2662ms tokens=3142 tool_calls=1
tool:deep_research: 0ms status=error (mismatched specialized inputs)
chat_turn: 1835ms tokens=3348 tool_calls=4
tool:url_extract: 0ms status=ok
tool:yt_transcript: 0ms status=ok
tool:web: 0ms status=error (fixture rejected unrelated query)
tool:python: 0ms status=ok
chat_turn: 1431ms tokens=3806 tool_calls=0
```

This is the clearest composite-tool contract failure: malformed meta-tool arguments
caused an inefficient partial fallback rather than one valid composite call.

### canary-semis-sector

```text
failed_checks:
  - suspicious AMD/MU price pattern
  - reply_generation_ms=41361, maximum=30000
  - total_tokens=26370, maximum=25000
  - stop_reason=deadline, required final_text

answer summary:
  NVDA +4.03% at $210.96; AMD +2.04% at $557.89; TXN +0.95% at $311.46;
  QCOM -1.02% at $189.16; LRCX -0.80% at $350.33. The reply correctly caveated
  missing/rate-limited quotes for much of the >$100B universe.

trace:
  chat_turn: 5045ms tokens=3166 tool_calls=3
  tool:quote NVDA: 2672ms status=ok
  tool:quote AMAT: 2499ms status=error (provider credits exhausted)
  tool:web: 2052ms status=ok
  chat_turn: 6245ms tokens=7386 tool_calls=1
  tool:deep_research: 17753ms status=ok
  chat_failure: 5281ms error_kind=transient
  chat_final: 4354ms tokens=9905 tool_calls=0
```

This is a live-integration and budget issue, not a provider-auth failure. The quote
provider's credit exhaustion reduced coverage; deep research then consumed too much of
the remaining deadline.

## Migrated Focus Cases

```text
fixture-earnings-comparison: fail 19/23 (correct facts, formatting/turn-budget misses)
fixture-channel-decision:   fail 22/23 (correct facts, superseded-plan mention)
canary-spacex-price:        pass 16/16 (quote + url_extract + web, 8.7s)
canary-semis-sector:        fail 13/17 (live quote coverage/deadline)
```
