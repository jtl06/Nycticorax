# Nycti

Nycti is a Discord bot for a private friend server. It answers when mentioned or replied to, can look things
up, run small calculations, fetch market data, summarize links/videos, set reminders, and keep a small amount
of opt-in memory.

Under the hood, Nycti is built as a bounded agent loop: it decides when to run the model, what context and tools
to expose, how tool results return to the model, and when to stop or recover from provider failures.

Requests are routed into quick, grounded, or deep profiles. Quick explanations avoid tool overhead, grounded
requests expose a focused tool bundle, and deep research gets larger reasoning/time budgets plus an evidence and
citation contract. Eligible self-contained deep web-research questions use a bounded composite path: the configured
When a cross-provider fallback is configured, its model plans and reduces the research directly; otherwise
`OPENAI_EFFICIENCY_MODEL` does so. The deep foreground model performs the cited synthesis. Users can override
automatic routing with `/depth`.

## What It Does

Nycti is meant to be useful in normal Discord conversations without processing every message. It supports:

- current web, image, URL, and YouTube transcript lookup
- stock quotes, recent price history, extended-hours data, and annual distribution/price comparisons
- bounded older Discord context retrieval when the recent window is not enough
- restricted Python calculations for small math/data tasks
- reminders and explicitly requested cross-channel messages
- selective long-term memory and compact user profiles
- operational debug logs, timing summaries, and built-in benchmarks

## Agent Control Loop

1. **Trigger gate:** Ignore ordinary server traffic. Run only for a mention, reply to Nycti, or slash command.

2. **Context assembly:** Build a small prompt from recent context, reply chains, linked messages, relevant
   images, and relevance-gated memory or date blocks.

3. **Answer and tool routing:** Select quick, grounded, or deep execution from deterministic request signals.
   Expose only relevant read tools for normal grounded requests. Eligible self-contained deep web-research requests
   first enter the composite path. Reminder and cross-channel send tools remain hidden unless the request explicitly
   authorizes them.

4. **Research or model turn:** Composite research uses the efficiency model to plan two to four focused queries,
   runs Tavily search and extraction concurrently, and uses the efficiency model again to reduce the evidence.
   Other requests go directly to the selected foreground model. The LLM client normalizes provider-specific
   request and tool-call differences.

5. **Tool execution:** Validate calls again, run independent calls concurrently, and return typed outcomes with
   status, latency, retryability, metrics, and provenance.

6. **Evidence and bounded continuation:** Normalize successful outcomes into stable evidence IDs. On a successful
   composite run, the deep foreground model receives the reduced evidence for one cited synthesis; a total
   composite failure falls back to the normal bounded tool loop. Reject invented URLs/citations, allow at most one
   repair, append a canonical source list, reject duplicate calls, and honor whole-request budgets.

7. **Finalization and telemetry:** If the loop exhausts its budget, run one tools-disabled final pass. Queue the
   ordered trace, usage, stop reason, and tool outcomes to a bounded background writer so persistence does not delay
   the visible reply.

Background memory extraction runs after the user-facing reply so optional memory work does not extend normal
response latency.

## Implementation Notes

### Bounded execution

`AgentRun` owns model-turn, tool-call, correction, continuation, and timeout budgets. The orchestrator has
explicit stop reasons for final text, duplicate calls, empty turns, exhausted budgets, deadlines, and provider
failures.

### Typed tool boundary

Each `ToolSpec` defines the native schema, handler, timeout, recovery guidance, and optional action permission.
Tool calls are validated at both selection and execution time. Exact argument signatures prevent repeated calls
without blocking materially different follow-up research.

### Composite deep research

Eligible self-contained deep questions with current-information or verification signals use a bounded composite
pipeline. Requests needing exact-URL, market, YouTube, calculation, Discord-context, or action tools stay on the
normal specialized-tool path.
When configured, the cross-provider fallback model plans two to four queries and reduces the gathered evidence;
otherwise `OPENAI_EFFICIENCY_MODEL` does so. Tavily searches and extracts the selected sources concurrently, then
the configured deep/foreground model produces the cited synthesis. If the composite pipeline returns no usable
evidence, Nycti retains the normal model-directed tool loop as its fallback.

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
Saying `bad bot` immediately after a recent Nycti reply posts a redacted replay bundle to the configured debug
channel with the original bounded prompt context, tool results, response, metrics, and correlated run steps.

The Discord lifecycle acknowledges slower requests with one editable progress message. `/cancel` stops the caller's
active request, while `/depth mode:quick|grounded|deep|auto` controls the quality/latency profile.

### Evaluation

The test harness uses fake model turns and tool outcomes to replay direct answers, multi-tool flows, duplicate
calls, partial failures, empty responses, finalization, and continuation.

The built-in slash-command evaluation commands cover deterministic regression checks and production canaries:

- `/benchmark earnings`: date-pinned, official-source NVIDIA-versus-AMD research and exact-value scoring
- `/benchmark context`: synthetic older-channel retrieval, decision tracking, ownership, open-question, and tool-policy scoring
- `/benchmark spacex`: live ticker/listing and current-price grounding canary
- `/benchmark semis`: live semiconductor universe and quote-coverage canary

All four report model/tool calls, token usage, and latency. Earnings and context use pinned fixtures for
repeatable scoring; SpaceX and semis intentionally exercise changing production data.

This keeps behavior changes measurable instead of relying only on subjective chat quality.

## Tooling

The current tool registry includes:

- Tavily web, image, and URL search/extraction
- Twelve Data quotes/recent history plus Yahoo extended-hours and annual price/distribution performance
- Chromium extraction for JavaScript-heavy pages
- YouTube transcript extraction and summarization
- bounded older Discord context retrieval
- restricted Python calculations
- reminders and explicitly authorized cross-channel sends

Nycti also supports multimodal context, selective long-term memory, compact user profiles, member/channel aliases,
table rendering, startup changelogs, and operational error reporting.

## Architecture

- `src/nycti/bot.py`: Discord trigger gate and reply lifecycle
- `src/nycti/chat/orchestrator.py`: bounded agent state machine
- `src/nycti/chat/run_state.py`: typed run, budget, permission, and outcome contracts
- `src/nycti/chat/tool_eligibility.py`: read-tool exposure and action permission policy
- `src/nycti/chat/tool_runner.py`: concurrent execution and typed outcomes
- `src/nycti/chat/tools/registry.py`: tool schemas, handlers, timeouts, and permissions
- `src/nycti/llm/`: provider request, fallback, circuit-breaker, and tool-call handling
- `src/nycti/chat/run_telemetry.py`: buffered correlated run persistence
- `src/nycti/memory/`: selective extraction, hybrid retrieval, profiles, and background writes
- `src/nycti/benchmarks.py`: deterministic research and Discord-context benchmark fixtures
- `src/nycti/discord/`: slash commands and operational views

PostgreSQL stores durable state and telemetry. The main tables cover settings, memories, reminders, aliases,
usage events, tool calls, agent steps, and message timing samples.

## Reliability Constraints

- Never invoke the LLM for every Discord message.
- Keep default context bounded and fetch older history only on demand.
- Never store raw channel history, secrets, credentials, or low-value chatter as memory.
- Require explicit intent for reminders and cross-channel sends.
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
models. `OPENAI_EFFICIENCY_MODEL` handles bounded deep-research planning and evidence reduction when no
cross-provider fallback is configured; otherwise those calls use `OPENAI_FALLBACK_CHAT_MODEL` directly.
`OPENAI_EFFICIENCY_REASONING_EFFORT` can keep primary-provider efficiency calls lighter.

## Useful Commands

- `/benchmark earnings`: evaluate the external research loop
- `/benchmark context`: evaluate older Discord-context retrieval and synthesis
- `/depth`: inspect or set automatic, quick, grounded, or deep answer routing
- `/cancel`: cancel your active request in the current channel
- `/logs`: inspect model, token, tool, and timing summaries
- `/show debug:true`: attach the detailed trace to your replies
- `/memories` and `/memory`: inspect or manage selective memory
- `/reminders`: inspect pending reminders
- `/channel` and `/nickname`: manage server-specific aliases

See `/help` in Discord for the complete command list.
