# Maintenance Loop

Use the `nycti-maintenance` skill on demand for triage, reviews, memory audits, or experiments. Its versioned source is
`skills/nycti-maintenance/SKILL.md`; the installed personal skill can be refreshed from that file.
There is no scheduled maintenance or background Codex follow-up. Use these tools within the task the user requests.

## Evidence Collection

Production switched to SQLite on 2026-09-14 (local time). The retained Postgres service
is a pre-cutover archive and must NOT be used for current incidents or memory audits.
`scripts/collect_maintenance_snapshot.py` and `scripts/read_bad_bot_feedback.py` are
still PostgreSQL-specific. The commands below apply only to PostgreSQL deployments.

For current production access, use the authorized Nycticorax SSH session and open
`file:/data/nycti.db?mode=ro` with Python's `sqlite3.connect(..., uri=True)`. Set
`PRAGMA query_only=ON`, use bounded parameterized SELECTs, and save any sensitive
output under local `.local/maintenance/` with mode 0600. Do not run migrations or
use a stale Postgres copy. For offline inspection, restore a recent verified S3
snapshot using [the SQLite restore guide](sqlite-migration.md), noting its timestamp.
The volume is not available to a local `railway run` process; use SSH or a restored
copy instead. Never copy an active `.db` without its WAL-aware backup operation.

Run from the repository root, using an installed Python with psycopg:

```bash
railway deployment list --service Nycticorax --limit 2 --json
railway logs --service Nycticorax --since 24h --lines 100 --filter '@level:error OR @level:warn'
railway run --service Postgres python3 scripts/collect_maintenance_snapshot.py --guild-id 1448835634725912738 --hours 24 --limit 20 --output .local/maintenance/latest.json
```

On this Mac, use `/Users/jacenli/anaconda3/bin/python3.11` if `python3` lacks the repository dependencies.
Use `--include-bundles` only to inspect selected failures. Snapshots use database-enforced read-only transactions
and a statement timeout. They do not run migrations, delete feedback, or send messages. If remote access fails,
report the blocker; do not broaden permissions or substitute stale reports as fresh evidence.

Write dated triage reports under `.local/maintenance/`. Track reviewed incident IDs in a local JSON index so
repeated reports do not create duplicate work. Preserve evidence and mark items reviewed rather than deleting logs.
Report memory statistics separately from semantic conclusions: never-retrieved does not mean useless or wrong.

## Live Resource Profiles

After registering an authorized Railway SSH public key and establishing host-key trust:

```bash
railway ssh --service Nycticorax --environment production -- python -m nycti.resource_profile --pid 1
railway logs --service Nycticorax --since 5m --lines 20 --filter resource_profile
```

The CLI verifies the target is a `nycti.main` process with a registered SIGUSR1 handler before signaling.
Do not send raw signals to an older deployment without this handler. The existing running process logs
one detailed snapshot, rate-limited to once per 15 seconds; no network listener or heap dump is added.
Missing Linux counters are omitted on unsupported platforms rather than reported as zero.

`RESOURCE_PROFILE_INTERVAL_SECONDS=300` also samples through the existing reminder poll, without a second
background task. Set 0 to disable periodic sampling; 60-3600 selects another interval, with the reminder poll
setting providing the minimum actual cadence. Each sample emits a small `resource_sample` log and the last
120 numeric samples stay in RAM. History resets on restart; Railway logs provide the longer retention window.
The on-demand snapshot contains recent samples and min/peak/change summaries for all RSS samples and tracked-idle
samples separately. Tracked idle means no active foreground requests or tracked background jobs, not guaranteed
zero OS activity. Sampled peaks may miss short bursts; process VmHWM remains the kernel's lifetime RSS peak.

Additional counters show CPU seconds, default-executor thread/queue counts (advisory), file descriptors, and
glibc allocator arena/in-use/free/mmap bytes when available. Allocator free bytes are retained native allocator
space, not a guaranteed reclaimable amount; these counters do not equal Python live-object size or RSS and must
not be added to RSS. The collector never calls malloc_trim or gc.collect. Capture duration is recorded per sample.
Use `railway logs --service Nycticorax --since 1h --lines 100 --filter resource_sample` for the compact history.

Compare process RSS/anonymous memory separately from container memory: container file cache and temporary
SSH probe processes can inflate the latter. `text_shallow_bytes` is only shallow retained string storage,
not the total object graph; strings may also be shared across reported collections. Queue capacity is a
limit, not an allocation. Counters contain no prompts, response text, identities, tokens or connection URLs.
The profiler does not prune caches, force GC, fetch the database or call a model. Save raw log snapshots
under `.local/maintenance/` and compare warmed, similarly idle periods before claiming a resource reduction.

## Temporary Allocation Attribution

After deploying the tracing-capable runtime, an authorized Linux/Railway shell can run:

```bash
python -m nycti.resource_profile --pid 1 --trace start
# Exercise one representative request, then let its background work settle.
python -m nycti.resource_profile --pid 1 --trace snapshot
python -m nycti.resource_profile --pid 1 --trace stop
```

Read `allocation_trace` entries from Railway logs. Start takes a one-frame tracemalloc baseline; snapshot
compares current retained Python allocations with that baseline. Stop emits a final comparison and clears
tracing metadata and the baseline. A ten-minute event-loop timer stops the session automatically; repeating
start does not extend it. Runtime shutdown also clears owned tracing. An externally enabled tracer is never
taken over or stopped. Normal resource profiling remains available while tracing is off.

The CLI checks each action's registered handler: SIGUSR2 starts tracing, SIGUSR1 captures profiles/deltas,
and SIGRTMIN stops it on Linux. Do not send these signals manually to older deployments. On platforms without
SIGRTMIN, manual stop is unavailable; automatic timeout and shutdown cleanup still apply.

Only the top 15 growing file/line locations, block/byte deltas, elapsed/capture time, and tracer counters are
logged. There are no object values, source-code excerpts, full trace files or heap dumps. Profiler module
allocations are filtered from the delta ranking. Reported tracer metadata does not include every profiling
overhead, such as retained snapshots. Traced samples are excluded from the normal idle summary and have a
separate traced-RSS summary; overall process peaks still include them. After tracing stops, subsequent
samples are labeled and summarized separately as post-trace RSS until restart, because allocator pages
used by instrumentation may remain resident after tracing metadata is freed.

This only attributes Python allocations made AFTER activation. It does not retroactively explain the existing
anonymous heap or all native allocations. Use an isolated startup trace/native profiler for those questions.
Tracing can increase RAM and CPU usage and snapshots can briefly pause the event loop; keep sessions short.

## Reproduction and Review

Group incidents by demonstrated failure mechanism, not by a rigid keyword classifier. Prioritize repeated wrong
answers, missed tools, excessive latency, and unsafe memory scope. Preserve real tool observations; strip private
identities and secrets before turning examples into committed tests. Keep a held-out phrasing to detect overfitting.
Use unit/fixture tests when they can prove the problem without paid APIs. No extra system instructions that reveal
the expected answer should be added to a benchmark's user prompt.

Before an experiment, check git status and the deployed revision. Work in a separate clean worktree and record
the baseline commit. Review both the original failure and final diff. Do not merge or deploy unattended.
Triage is read-only; an explicitly requested improvement experiment may produce one reviewable candidate and its evidence.

## Timing Experiments

Read `maintenance-policy.json`: external API spend defaults to zero. `--mode fixture` freezes tool evidence but
still makes live model calls, so it is not a free/offline test. With a user-approved, bounded API budget, use:

```bash
PYTHONPATH=src python3 scripts/run_live_benchmarks.py --mode fixture --case-id CASE_ID --repeats 3 --results .local/maintenance/experiment/A.md --traces .local/maintenance/experiment/A-traces.md --write-baseline .local/maintenance/experiment/A.json
PYTHONPATH=src python3 scripts/run_live_benchmarks.py --mode fixture --case-id CASE_ID --repeats 3 --results .local/maintenance/experiment/B.md --traces .local/maintenance/experiment/B-traces.md --compare-baseline .local/maintenance/experiment/A.json
```

Create the output directory first. Use actual manifest IDs and record the sole variable changed between A and B.
For a timing claim, alternate paired A/B batches rather than trusting one sequential batch comparison. Preserve all
raw traces, including passing slow runs. Inspect per-case quality and medians; aggregate latency alone can hide
regressions. Keep benchmarkresults.md and benchmarkresult_traces.md current only after an actual benchmark run,
with links to the complete archived experiment. Provider timing is not Discord end-to-end timing.

An experiment report states: hypothesis, revision/configuration, cases, samples, pass/check rates, per-case latency,
observed spend, trace paths, review findings, and accept/reject/inconclusive. An inconclusive result is useful.

## On-Demand Requests

Examples: "check recent bad-bot logs", "audit memory", "review the latest change", or "test a latency improvement".
Do only the requested work; don't launch the other modes or create a schedule automatically. Report findings and
uncertainty in the current conversation. Production writes, merges and reset-credit redemption need separate authorization.
