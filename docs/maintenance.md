# Maintenance Loop

Use the `nycti-maintenance` skill on demand for triage, reviews, memory audits, or experiments. Its versioned source is
`skills/nycti-maintenance/SKILL.md`; the installed personal skill can be refreshed from that file.
There is no scheduled maintenance or background Codex follow-up. Use these tools within the task the user requests.

## Evidence Collection

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
one snapshot, rate-limited to once per 15 seconds; no network listener, periodic profiler or heap dump is added.
Missing Linux counters are omitted on unsupported platforms rather than reported as zero.

Compare process RSS/anonymous memory separately from container memory: container file cache and temporary
SSH probe processes can inflate the latter. `text_shallow_bytes` is only shallow retained string storage,
not the total object graph; strings may also be shared across reported collections. Queue capacity is a
limit, not an allocation. Counters contain no prompts, response text, identities, tokens or connection URLs.
The profiler does not prune caches, force GC, fetch the database or call a model. Save raw log snapshots
under `.local/maintenance/` and compare warmed, similarly idle periods before claiming a resource reduction.

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
