# Python Harness Redesign: Capability First

Status: proposal, not an approved full rewrite or a second running implementation.
Stay in Python. Keep the SQLite cutover independent of agent behavior changes so
database regressions and answer-quality regressions have different rollback points.

## What We Already Have

Do not rebuild these just to give them new names:

- `chat/run_state.py`: typed run state, tool outcomes, deadlines, correction budgets.
- `chat/orchestrator.py`: a bounded answer-or-tool loop.
- `chat/tool_runner.py`: parallel execution with ordered results.
- `chat/tools/registry.py`: native schemas, parsers, handlers and resource policy.
- `chat/evidence.py`: bounded source evidence with stable IDs.
- `runtime.py`: shared dependency construction for the current bot.
- `live_benchmarks.py`, `live_benchmark_discord.py`: existing fixtures and Discord-context replays.
- `memory/` and `procedures/`: visibility, lifecycle, selective writes and provisional learning safeguards.

The worthwhile rewrite is of ownership boundaries and information flow. A different
language, more modules, or an additional always-on planner would not establish better answers.

## First: Database Lifetime Boundaries

The immediate SQLite risk is concrete: `bot.py::_generate_reply` passes an open
session into `ChatContextBuilder.prepare`. That builder reads settings/identities,
then can await a query embedding in `chat/context.py`. The connection remains
checked out while external work finishes. The proposed one-connection SQLite pool
would make unrelated telemetry, reminders, or memory requests wait behind it.

The candidate now releases preflight sessions before embeddings for chat context
and the memory-search tool, then rechecks settings during retrieval. Tests cover
connection availability, opt-out during the wait and cancellation. The broader
read/compute/write architecture below remains a proposal:

1. Load bounded input records in a short read transaction into detached immutable
   values, then release the connection.
2. Perform embeddings and provider work outside transactions. Keep existing overlap
   with independent context collection; do not add an LLM routing call.
3. Reopen short transactions for retrieval counters, usage and final state changes.
   Recheck opt-in, visibility and source version before applying writes or including
   data whose access may have changed during the external wait.
4. Extend the same read/compute/write discipline to memory tools and administrative
   commands that call model helpers with a caller-owned session.

Acceptance: delayed fake embeddings must not block an independent DB transaction;
memory opt-out/retraction during the wait is respected; cancellation releases all
connections; concurrent quota settlement charges once. Measure queue wait separately
from SQL execution. Do not solve this by disabling foreign keys or durability.

## Extract One Headless Entry Point

`runtime.py` currently constructs a Discord bot, and both smoke/benchmark runners
call its private `_generate_reply`. `ChatOrchestrator.__init__` also constructs the
tool executor, research service and telemetry writer. These dependencies make a
small isolated core harder to exercise and profile than it needs to be.

Introduce one application-level `respond(request) -> result`, used by the Discord
adapter, smoke CLI and benchmark runner. A request carries bounded context, requester
scope, timestamp, attachments, explicit depth and cancellation; a result carries
the answer, stop reason, usage, evidence and ordered run events. The Discord adapter
owns triggering, authorization, fetching messages, progress edits and delivery only.

Move existing code rather than maintain two loops. Supply model, tool runner, clock
and event sink through explicit interfaces. Keep the existing composition root as
the production wiring point. An in-memory event sink must require neither Discord
login nor a real database. Type the result before extending debug dictionaries further.

Acceptance: the same captured request and scripted provider/tool transcript produces
the same result through bot, smoke and benchmark adapters; no hidden provider calls
or duplicate writes occur. Preserve cancellation and confirmation boundaries.

## Give the Model Better Evidence, Not More Policing

`ToolOutcome` currently carries text plus URLs; `EvidenceItem` mainly carries a source
and short excerpt. These are useful, but they do not encode a quote's observation
time, a history tool's covered range, or which requested entities were absent.

Extend the existing result contract incrementally with optional typed payloads and
metadata: source, observed time, covered time range, subject IDs, units, missing
coverage and structured provider errors. Start with quote/history because actual
incidents confused current price, session, historical highs and sector membership.
Retain text adapters for older tools during the transition.

Build one bounded model-facing view from the current request, ordered tool results
and evidence. Do not accumulate duplicate full evidence blocks. Keep raw artifacts
behind the existing privacy/expiry policy rather than putting them all into prompts.
Freshness metadata is evidence, not proof a source is correct. Citation checks do
not prove the answer's claims are supported.

Acceptance: dated listing changes, conflicting sources, short versus all-time
history and partial basket coverage improve on held-out fixtures. Do not embed the
expected conclusion or a forced tool command into user benchmark prompts.

## Simplify Recovery Only With Evidence

The current loop already has one bounded answer-repair path, duplicate/empty-turn
corrections, and finalization. Do not add a synthesis agent or assessor by default.
Make each transition observable, distinguish transport failures from unusable model
output, and retain a usable draft if an optional continuation fails.

Keep deterministic enforcement for permissions, tool validation, budgets and known
protocol errors. Move task semantics into clear evidence/prompt contracts. Remove
domain-specific corrective rules only when held-out tests show their replacement
works; a cleaner architecture alone is not grounds to delete a working safeguard.

## Later: Continuity, Not Unbounded Memory

After the headless runtime is stable, add bounded task-scoped working state for
multi-turn follow-through: references to evidence, unresolved questions and explicit
pending actions. Do not turn this into permanent raw channel history. Durable user
memory remains selective and permission-scoped; procedures remain provisional until
explicitly validated. Resumable jobs need expiry, cancellation, idempotent actions
and schema versioning before they are enabled.

## Rollout / Deletion Plan

1. Finish SQLite migration/restore validation and short transaction boundaries.
2. Extract the headless request/result boundary without changing model prompts.
3. Extend quote/history result metadata and compare equivalent real-user replays.
4. Unify event/result formatting; delete superseded debug-dictionary adapters after
   every consumer is migrated, not before.
5. Reassess recovery rules and task working state against measured failures.

Delete duplicate wiring/private bot entry points after adapter parity is demonstrated.
Avoid a plugin platform, event-sourcing database, second migration runner, or generic
multi-agent framework unless a concrete capability needs it.

Each behavioral change needs old failure cases plus held-out variations, preserved
raw traces and paired quality/latency comparisons. Offline scripted runs verify the
harness, not model quality. Real-model evaluations require a separately approved API
budget; the current maintenance budget is zero. Never call synthetic latency the
production E2E baseline. The criteria are fewer unsupported claims, fewer wasteful
turns, preserved privacy/permissions, and no material latency regression.
