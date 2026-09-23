# Grounding, Latency and Idle Memory Candidate

2026-09-21, based on `0450e3f`. Branch: `codex/grounding-latency-memory`.
Production access during preparation and benchmarking was read-only. The user authorized commit/push
on 2026-09-22 after reviewing the results; no Railway configuration change is included.

## Evidence and Changes

- The reviewed overnight report had overnight prices in its quote result but repeated regular closes.
  Present the extended-session block first; distinguish regular-day and extended-session percentages,
  honor explicit closing requests, and avoid claiming all returned quotes/valuations are contemporaneous.
- Announcement guidance now distinguishes earlier previews from published results. Intraday explanations
  must match the move's time window and label causal hypotheses. No ticker/event-specific routing was added.
- Successful valuation metadata was incorrectly recorded as a quote error; this is fixed.
- Foreground calls replaced the 30-second provider limit with the full remaining work budget. Preserve
  the provider limit and enforce wall time so configured fallback can use remaining time. Overall budgets
  and model/reasoning settings are unchanged. Cancellation records elapsed time and still propagates.
- Add opt-in `IO_MAX_WORKERS`, default 0 (unchanged Python behavior), and executor capacity/queue samples.
  Do not trim the allocator, force garbage collection, restart production, or weaken SQLite durability.

## Verification

- Four focused regressions failed against the baseline and pass against this candidate, using fake SDK calls.
- Full `scripts/check_ci.py`: Ruff, selected mypy checks, compileall, and **1,064 tests passed**.
- Two short frozen model cases added: `ACME overnight?` and `How did ACME close yesterday?`.
  These were validated as fixtures but **not run against a model**. Prompt changes are not proof of better answers.
- Worker experiment: 12 isolated local subprocess samples, alternating 32/24-worker order, three pairs per
  workload. All outputs matched. In the 48-read overlap case, median retained RSS fell about 10.6 MiB,
  while median latency rose about 5.8%. Keep the smaller pool opt-in; macOS synthetic data cannot establish
  Linux/Railway savings. See `benchmarkresults.md` and the full raw `benchmarkresult_traces.md` dump.
- Second-pass diff review, not an independent review: kept unavailable extended-session errors after the
  valid regular quote, retained deep-research timeout overrides, and left production concurrency unchanged.

## Remaining Uncertainty

The 43-second incident's timed-out model step recorded only 309 ms. Roughly 25 seconds remain unattributed;
the trace does not establish provider cancellation as the cause. Cancellation accounting is independently
reproduced, not a claimed diagnosis of that incident. No production e2e/RSS improvement is claimed yet.
The initial checks above were offline. The user subsequently approved a one-request $2 API budget;
the paired live-model follow-up, including benchmark-fixture corrections, is recorded in
`benchmarkresults.md` and `benchmarks/results/2026-09-22-market-ab/`. The standing policy remains $0.
