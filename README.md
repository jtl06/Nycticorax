# Nycti

Nycti is a Discord bot for a private friend server. It answers mentions and replies by default, with optional
explicit-name and allowlisted ambient invocation. It can look things up, run small calculations, fetch market data,
summarize links/videos, set reminders, and keep a small amount of opt-in memory.

Under the hood, Nycti is built as a bounded agent loop: it decides when to run the model, what context and tools
to expose, how tool results return to the model, and when to stop or recover from provider failures.

Requests are routed into quick, grounded, or deep profiles, but routing changes model, reasoning, token, turn, and
time budgets—not access to grounding. Every configured safe read tool remains callable; heuristics only add
nonbinding relevance hints. The model can invoke bounded `deep_research`, which uses an economy model to fan out
across web evidence and can combine exact URLs, live finance, transcripts, and calculations. Users can override
automatic quality/latency routing with `/depth`.

## What It Does

Nycti is meant to be useful in normal Discord conversations without processing every message. It supports:

- current web, image, URL, and YouTube transcript lookup
- stock quotes with typed personal/shared watchlists, complete basket updates, public-company valuation inputs,
  recent/long-range history, extended-hours data, and annual distribution/price comparisons
- bounded older Discord context retrieval when the recent window is not enough
- restricted Python calculations and graph analysis with allowlisted `math`, `statistics`, `numpy`, and `networkx`
- reminders and explicitly requested cross-channel messages
- retained Discord member names for resolving natural in-channel address/ping requests
- selective long-term memory and compact user profiles
- operational debug logs, timing summaries, and built-in benchmarks

## Agent Control Loop

1. **Trigger gate:** Ignore ordinary server traffic by default. Mention/reply invocation is the compatible default;
   deployments can also enable a leading explicit name or conservative ambient questions in allowlisted channels.
   Ambient invocation applies deterministic scope/rate gates, then a tiny economy-model addressedness classifier;
   it rejects messages aimed at other users, fails closed, and has a per-user/channel cooldown.

2. **Context assembly:** Build a small prompt from recent context, reply chains, linked messages, relevant images,
   matching retained member identities, and relevance-gated memory or date blocks. Active market watchlists are
   loaded as compact typed state instead of competing with prose during snapshot compaction. A complete Discord
   cache avoids REST; partial cache windows are merged with fetched history.

3. **Answer and tool routing:** Select quick, grounded, or deep budgets from deterministic request signals. Keep all
   configured safe reads directly reachable and use routing signals only as nonbinding promotion hints. In guild
   requests, reminder and cross-channel tools are proposal-only capabilities; prompt wording never grants a write.

4. **Model turn:** The selected foreground model can answer or call one or more tools. `deep_research` is a normal
   model-callable meta-tool, not a regex-forced prepass. It uses the economy provider/model to plan two to four
   focused queries, searches and extracts concurrently, reduces evidence, and can run bounded specialized inputs
   alongside that web work.

5. **Tool execution:** Validate calls again, run independent calls concurrently, and return typed outcomes with
   status, latency, retryability, metrics, provenance, and auxiliary usage. Action calls only create an exact,
   server-rendered proposal. The same user must run `/confirm` in the same guild/channel before a short-lived,
   single-use capability executes it; channel permissions and dynamic conditions are rechecked at execution.

6. **Evidence and bounded continuation:** Normalize successful outcomes into stable evidence IDs. Reject invented
   URLs/citations, append a canonical source list, reject duplicate calls, and honor whole-request budgets.
   Duplicate-tool, quote-verification, watchlist-completeness, empty-output, and evidence-repair recovery are each
   one-shot under a global correction cap.

7. **Finalization and telemetry:** If the loop exhausts its budget, run one tools-disabled final pass. Queue the
   ordered trace, usage, stop reason, and tool outcomes to a bounded background writer so persistence does not delay
   the visible reply.

Background memory extraction runs after the user-facing reply through a bounded 64-entry FIFO queue and one worker,
so optional memory work does not extend normal response latency or fan out into concurrent model calls during a
burst. The worker uses `OPENAI_MEMORY_MODEL`; when that variable is unset it inherits `OPENAI_EFFICIENCY_MODEL`.
The queue is intentionally in-process; pending optional jobs are discarded during shutdown rather than delaying a
restart or adding a durable job table.

## Implementation Notes

### Bounded execution

`AgentRun` owns model-turn, weighted tool-cost, deep-research, correction, continuation, and timeout budgets. The orchestrator has
explicit stop reasons for final text, duplicate calls, empty turns, exhausted budgets, deadlines, and provider
failures.

### Typed tool boundary

Each `ToolSpec` defines the native schema, handler, timeout, and recovery guidance. Runtime capability checks remove
only tools whose provider or request context is unavailable. Exact argument signatures prevent repeated calls
without blocking materially different follow-up research. The current catalog is small enough to keep every safe
read direct; `AnswerPlan` already distinguishes direct and deferred exposure for a future catalog resolver without
hiding tools today.

### Composite deep research

The model can call `deep_research` whenever one lookup is insufficient. When configured, the cross-provider fallback
model plans two to four queries and reduces the gathered evidence; otherwise `OPENAI_EFFICIENCY_MODEL` does so.
Tavily searches and extracts selected sources concurrently. One meta-tool call can also include up to three exact
URLs, five live market symbols, two YouTube transcripts, and two restricted calculations. The model may instead—or
also—call any specialized read tool directly. Exact/specialized evidence is retained ahead of broad web reductions.
Deep research is limited to one weighted call per run and two concurrent calls across the bot. If composite work is
thin or fails, the normal loop remains available.

### Memory visibility and retrieval

Memory is tiered: the system prompt supplies fixed core behavior, bounded per-user and per-guild snapshots provide
warm continuity on every opted-in request, query-specific retrieval supplies the hot working set, and typed Postgres
rows remain the durable source of truth. Snapshot compaction ranks active facts by confidence, kind, reinforcement,
retrieval use, recency, and durable category. Valid consolidated summaries may replace their source bullets in the
snapshot, while expired, superseded, redundant, weak, or long-unused rows leave prompt context first. This eviction
never deletes the source row; hybrid retrieval can still recover an omitted durable fact. Private rows are eligible
only for their owner's snapshot, and guild snapshots accept only opted-in `guild_shared` or `lore` rows.

Automatically extracted personal facts remain `private` and readable only by their owner. Explicit future
server defaults, such as a shared market-report watchlist, may be stored as `guild_shared`; explicitly stated
server-wide conventions or recurring lore may be stored as `lore`. The local policy never auto-shares ordinary
personal preferences, plans, profiles, holdings, or sensitive facts. An owner can still use
`/memory memory_id:<id> visibility:<scope>` to mark a memory `guild_shared` or `lore`; both shared scopes are readable
only inside that memory's guild.
Postgres remains the source of truth. Durable memories carry typed subject/predicate/value fields, fact/episode/
working/lore/summary layers, validity dates, lifecycle status, reinforcement counts, supersession links, and bounded
entity relationships. Repeated facts reinforce confidence; changed or explicitly retracted facts end the previous
version instead of leaving conflicting active rows. Explicit temporary memory expires automatically.
Explicit stable stock-ticker interests are stored as separate private facts per user and symbol. This lets one user
follow several tickers without overwriting another user's interests, while holdings, transactions, position sizes,
cost basis, balances, and inferred symbols remain excluded. Explicit shared market-report tickers use separately
keyed guild-visible facts so later additions do not erase the rest of the server list. Active ticker facts are also
materialized into a dedicated prompt block, so snapshot eviction cannot silently remove a required watchlist member.

Retrieval enforces requester, owner, guild, and visibility constraints in the database query and again before
returning results. It combines semantic, lexical, entity, recency, confidence-decay, and reinforcement signals,
diversifies layers, and chooses a 2-to-configured-limit context budget from the request. The opted-in caller's compact
profile and a cheap lexical memory lookup are available on every reply; semantic retrieval is added only when the
request benefits from it, and the model can use `memory_search` for deeper recall. Historical rows are excluded unless
the prompt asks about prior state. Background prefetch combines caller-private matches with labeled same-guild
shared/lore matches. Individual memories are capped at 320 characters, source excerpts at 600 characters, and the
personal profile at 1,600 characters. A cooldown-bound background consolidator considers up to 24 recent durable
facts and can add one derived overview while retaining its source fact IDs; it never runs on the foreground reply path.
Periodic maintenance also removes deterministically sensitive legacy rows and profile lines, fills missing lifecycle
metadata, and conservatively promotes legacy market-report defaults without broadening unrelated memory access.

### Provider resilience

`OpenAIClient` supports OpenAI-compatible providers through explicit capability and error policies. It handles
token-field differences, native-tool incompatibility, fallback models, cooldown circuit breakers, transient
failures, and inline tool-call compatibility without treating every `403` as the same error.

For stateless OpenAI Responses calls, Nycti requests encrypted reasoning state, replays complete response items
across tool and continuation turns, distinguishes hidden reasoning from visible output tokens, and handles refusal,
incomplete, and API-level failure states before marking a provider attempt healthy.

### Observability

Each run receives a correlation ID. Nycti records ordered model, tool, and finalization steps with:

- requested and active models
- provider attempts and recovery paths
- prompt, completion, and total tokens
- tool argument hashes, status, latency, and provenance
- stop reason and end-to-end timing

`/logs` renders compact summaries, while per-message debug mode exposes the detailed agent trace.
Replying `bad bot` directly to a recent Nycti response posts a redacted replay bundle to the configured debug
channel. Users can also describe a concrete problem naturally; Nycti can call `report_issue`, archive its latest
response, and continue with the correction. Bundles contain the original bounded prompt context, tool results,
response, metrics, and correlated run steps. These snapshots stay only in the bot's 15-minute in-memory cache by
default. If
`PERSIST_BAD_BOT_DIAGNOSTICS=true`, Nycti writes a redacted, expiring snapshot immediately after each response—
before anyone submits feedback—so either feedback path can survive a restart. Persistent rows include bounded
conversation and tool-result text, carry a 15-minute expiry, and are removed on startup and subsequent
diagnostic reads/writes after expiry. Submitted or self-reported bundles are archived in Postgres without an expiry
before Nycti tries to post them to Discord's debug channel.

Review and clear archived feedback through Railway with:

```bash
railway run --service Postgres python3 scripts/read_bad_bot_feedback.py --clear
```

The command deletes only rows displayed by that run. Add `--full` for raw agent messages, schemas, and traces.

The Discord lifecycle acknowledges slower requests with one editable phase-based progress bar. It follows context,
model, tool, composition, and delivery milestones, then becomes the final reply. `/cancel` stops the caller's active
request, while `/depth mode:quick|grounded|deep|auto` controls the quality/latency profile.

### Evaluation

The test harness uses fake model turns and tool outcomes to replay direct answers, multi-tool flows, duplicate
calls, partial failures, empty responses, finalization, and continuation.

`benchmarks/routing_cases.json` is a labeled routing regression corpus covering prior freshness misses, false
positives, multilingual prompts, memory/deep-research promotion, and novel product/version wording. The evaluator
measures exposure, promotion and call misses, latency, and grounded-answer citation quality. Runtime telemetry also
preserves unavailable promotions, distinguishes unrelated calls from useful grounding, and marks expected answers
that produced no evidence instead of silently leaving them unscored.

The built-in slash-command evaluation commands cover deterministic regression checks and production canaries:

- `/benchmark suite`: run the short-prompt real-model suite against pinned fixture tools by default, or opt into
  changing production canaries; every attempt gets a compact database record and failures retain a bounded,
  redacted replay trace for 90 days
- `/benchmark failures`: list recent failed/error attempts, with `/benchmark trace` available to download one
  stored replay bundle

Focused cases now live in the same manifest suite: `fixture-earnings-comparison` preserves exact official-source
NVIDIA/AMD scoring, `fixture-channel-decision` covers ownership and open questions, and the
`canary-spacex-price` and `canary-semis-sector` cases exercise live listing and broad quote grounding.

The suite manifest lives in `benchmarks/live_cases.json`; its prompts are capped at 120 characters and its primary
checks are deterministic. It covers every current read tool, private/shared/lore memory scopes, composite mixed-source
research, and explicit latency/turn/combined-model-token budgets. Fixture mode still runs the configured foreground
LLM against a
frozen fixture clock, but gives it stable tool results, so answer/tool-routing regressions are reproducible. Canary
mode uses real search, extraction, finance, and composite
research providers, and grades grounded behavior rather than freezing volatile facts. Runs are isolated from member
profiles, aliases, channel history, and memory writes. They are manual and admin-only because they spend real model
tokens. `repeats` can expose flaky behavior, and each failed attempt is retained even if another repetition passes.
Every completed suite returns a downloadable Markdown batch report. If a long run outlives Discord's interaction
token, Nycti posts that report in the invoking channel instead. The checked-in `benchmarkresults.md` and
`benchmarkresult_traces.md` are point-in-time snapshots; runtime suites do not mutate the deployed checkout. Fixture
cases may include bounded synthetic profile or memory context to test ownership and recall through the normal agent
prompt without reading production user data.

The ordinary pytest suite never makes live model calls. It tests the manifest, runner, scorers, fixtures, persistence,
redaction, and command plumbing with scripted results; use `/benchmark suite` when you intentionally want production
LLM traffic.

For an isolated local run using configured environment credentials, run
`PYTHONPATH=src python scripts/run_live_benchmarks.py --mode all`. It uses temporary SQLite state and refreshes the
checked-in result summary plus raw failed/error traces without connecting to Discord or writing production data.
Repeat `--case-id CASE` to run a focused group while debugging a specific behavior.

This keeps behavior changes measurable instead of relying only on subjective chat quality.

## Tooling

The current tool registry includes:

- Tavily web, image, and URL search/extraction
- Twelve Data quotes/recent history plus Yahoo extended-hours and annual price/distribution performance
- Chromium extraction for JavaScript-heavy pages
- YouTube transcript extraction and summarization
- bounded older Discord context retrieval
- restricted Python calculations and graph analysis (`math`, `statistics`, `numpy`, and `networkx` only)
- model-callable composite deep research and requester-scoped memory search
- server-validated reminder and cross-channel-message proposals with `/confirm`

Nycti also supports multimodal context, selective long-term memory, compact user profiles, member/channel aliases,
table rendering, startup changelogs, and operational error reporting.

## Architecture

- `src/nycti/bot.py`: Discord trigger gate and reply lifecycle
- `src/nycti/chat/orchestrator.py`: bounded agent state machine
- `src/nycti/chat/run_state.py`: typed run, budget, exposure, correction, and outcome contracts
- `src/nycti/chat/tool_eligibility.py`: budget selection plus nonbinding tool-promotion hints
- `src/nycti/chat/action_confirmation.py`: exact proposals and short-lived single-use capabilities
- `src/nycti/chat/tool_runner.py`: concurrent execution and typed outcomes
- `src/nycti/chat/tools/registry.py`: tool schemas, handlers, timeouts, and recovery guidance
- `src/nycti/llm/`: provider request, fallback, circuit-breaker, and tool-call handling
- `src/nycti/chat/run_telemetry.py`: buffered correlated run persistence
- `src/nycti/memory/`: selective extraction, hybrid retrieval, profiles, and background writes
- `src/nycti/live_benchmarks.py`: short-prompt real-model suite, fixture tools, and deterministic scoring
- `src/nycti/live_benchmark_storage.py`: expiring attempt summaries and redacted failure replay bundles
- `src/nycti/discord/`: slash commands and operational views

PostgreSQL stores durable state and telemetry. The main tables cover settings, memories, reminders, aliases,
usage events, tool calls, agent steps, message timing samples, and live-benchmark attempts.

## Reliability Constraints

- Never invoke the LLM for every Discord message.
- Keep default context bounded and fetch older history only on demand.
- Never store raw channel history, secrets, credentials, or low-value chatter as memory.
- Never derive write authority from arbitrary prompt text; require an exact server proposal and `/confirm`.
- Keep optional extraction and profile work off the foreground reply path.
- Track approximate usage and latency for every model call.

## Tests

```bash
PYTHONPATH=src python3 -m pytest tests/
```

The suite covers the control loop, provider policies, tool parsing and execution, context assembly, memory,
market/search integrations, Discord formatting, and benchmark scoring.

## Local Setup

Requirements: Python 3.11+, PostgreSQL, and a Discord bot with Message Content Intent enabled.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m nycti.main
```

If you enable Chromium extraction, also run `pip install -e ".[browser]"` followed by
`python -m playwright install chromium`.

For Docker:

```bash
cp .env.example .env
docker compose up --build
```

Set the same long random PostgreSQL password in `POSTGRES_PASSWORD` and `DATABASE_URL` before starting Compose.
PostgreSQL is reachable only on the Compose network; the bot container runs as a non-root user with
`no-new-privileges`.

Configuration is documented in [`.env.example`](.env.example). At minimum, set the Discord token, database URL,
chat provider credentials, and chat model. Tavily, Twelve Data, embeddings, vision, browser extraction, and the
debug channel are optional integrations. `OPENAI_FALLBACK_API_KEY`, `OPENAI_FALLBACK_BASE_URL`, and
`OPENAI_FALLBACK_CHAT_MODEL` optionally route model calls to a separately authenticated provider after the primary
provider's retry and same-provider fallbacks are exhausted. `OPENAI_REASONING_EFFORT` controls supported
reasoning models; optional `OPENAI_QUICK_MODEL` and `OPENAI_DEEP_MODEL` route those answer profiles to dedicated
models. `OPENAI_DAILY_TOKEN_BUDGETS` accepts comma-separated `model=token-limit` pairs; once a configured model's
daily budget is consumed, calls use `OPENAI_DAILY_TOKEN_FALLBACK_MODEL` with
`OPENAI_DAILY_TOKEN_FALLBACK_REASONING_EFFORT` (high by default). `OPENAI_EFFICIENCY_MODEL` handles bounded
deep-research planning and evidence reduction when no
cross-provider fallback is configured; otherwise those calls use `OPENAI_FALLBACK_CHAT_MODEL` directly.
`OPENAI_EFFICIENCY_REASONING_EFFORT` can keep primary-provider efficiency calls lighter.
`OPENAI_MEMORY_MODEL` may select a separate slower or cheaper queued-memory model; if omitted, it inherits
`OPENAI_EFFICIENCY_MODEL` and then falls back to `gpt-4.1-nano`.
Memory depth is bounded by `MEMORY_RETRIEVAL_LIMIT` (6 by default, maximum 12).
`MEMORY_CONFIDENCE_HALF_LIFE_DAYS` controls ranking decay;
`MEMORY_CONSOLIDATION_MIN_MEMORIES` and `MEMORY_CONSOLIDATION_COOLDOWN_SECONDS` bound asynchronous overview
generation. `MEMORY_USER_SNAPSHOT_MAX_CHARS` and `MEMORY_GUILD_SNAPSHOT_MAX_CHARS` bound the always-available
warm memory caches materialized from active typed facts. Snapshot eviction affects prompt residency only; durable
facts remain in Postgres for hybrid retrieval. `MEMORY_RETENTION_NEVER_RETRIEVED_DAYS` and
`MEMORY_RETENTION_STALE_RETRIEVED_DAYS` set the base
cleanup windows. Durable facts, lore, summaries, and reinforced memories receive twice those windows, while expired
or superseded history uses the base window. These settings do not weaken visibility checks or enable memory for users
who opted out.

`PERSIST_BAD_BOT_DIAGNOSTICS` is `false` by default. Enabling it persists the bounded diagnostic content
described above before feedback is submitted; leave it disabled if restart-surviving feedback is not worth that
temporary storage tradeoff. Explicit `bad bot` feedback is always retained as a redacted diagnostic archive.

Discord invocation is configured with `DISCORD_INVOCATION_MODES`, a comma-separated combination of
`mention_reply`, `explicit_name`, and `ambient`. The default is `mention_reply`. `explicit_name` recognizes only a
leading direct address using `DISCORD_INVOCATION_NAME`; it does not remove later uses of the name from the prompt.
`ambient` requires `DISCORD_AMBIENT_CHANNEL_IDS`; after deterministic scope and rate gates, a bounded
`OPENAI_EFFICIENCY_MODEL` classifier decides whether a message is a standalone assistant-suitable question/request.
It is subject to `DISCORD_AMBIENT_COOLDOWN_SECONDS` per user and channel. Bots, DMs, other guilds, replies to people,
and messages mentioning another member fail closed. For example:

```dotenv
DISCORD_INVOCATION_MODES=mention_reply,explicit_name,ambient
DISCORD_INVOCATION_NAME=Nycti
DISCORD_AMBIENT_CHANNEL_IDS=123456789012345678,234567890123456789
DISCORD_AMBIENT_COOLDOWN_SECONDS=30
```

## Useful Commands

- `/benchmark suite [mode:<fixtures|canaries|all>] [case_id] [repeats:<1-3>]`: run real-LLM evaluations
- `/benchmark failures [limit]`: list recent failed or errored live evaluations
- `/benchmark trace failure_id:<id>`: download a stored redacted failure trace
- `/depth`: inspect or set automatic, quick, grounded, or deep answer routing
- `/cancel`: cancel your active request in the current channel
- `/logs`: inspect model, token, tool, and timing summaries
- `/show debug:true`: attach the detailed trace to your replies
- `/memories` and `/memory`: inspect or manage selective memory
- `/confirm`: execute one exact, unexpired action proposal
- `/reminders`: inspect pending reminders
- `/channel` and `/nickname`: manage server-specific aliases

See `/help` in Discord for the complete command list.
