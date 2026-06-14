# Nycti

Nycti is a Discord agent built to explore the engineering behind reliable agentic systems: bounded control
loops, selective context, tool execution, provider recovery, telemetry, and long-running state.

The Discord bot is the product surface. The main work is the harness that decides when the model runs, what
context and tools it receives, how tool results return to the model, and when the loop must stop.

## Engineering Focus

Nycti demonstrates how to:

- turn an LLM call into a typed, bounded agent loop
- expose only tools relevant to the current request
- execute independent tool calls concurrently
- prevent duplicate, unauthorized, or runaway tool use
- recover from empty responses, truncation, timeouts, and provider-specific failures
- keep foreground latency separate from background memory work
- trace each model and tool step with correlated token, latency, and outcome data
- evaluate the full loop with deterministic replay tests and a production-style benchmark

## Agent Control Loop

1. **Trigger gate:** Ignore ordinary server traffic. Run only for a mention, reply to Nycti, or slash command.

2. **Context assembly:** Build a small prompt from recent context, reply chains, linked messages, relevant
   images, and relevance-gated memory or date blocks.

3. **Tool eligibility:** Select tool schemas from request intent. Action tools such as reminders and
   cross-channel sends also require explicit permission flags.

4. **Model turn:** The model returns an answer or structured tool calls. The LLM client normalizes
   provider-specific request and tool-call differences.

5. **Tool execution:** Validate calls again, run independent calls concurrently, and return typed outcomes with
   status, latency, retryability, metrics, and provenance.

6. **Bounded continuation:** Feed outcomes back to the model. Reject exact duplicates, allow one empty-turn
   correction, and stop at model-call, tool-call, and wall-clock budgets.

7. **Finalization and telemetry:** If the loop exhausts its budget, run one tools-disabled final pass. Persist
   the ordered trace, usage, stop reason, and tool outcomes in one buffered transaction.

Background memory extraction runs after the user-facing reply so optional memory work does not extend normal
response latency.

## Harness Design

### Bounded execution

`AgentRun` owns model-turn, tool-call, correction, continuation, and timeout budgets. The orchestrator has
explicit stop reasons for final text, duplicate calls, empty turns, exhausted budgets, deadlines, and provider
failures.

### Typed tool boundary

Each `ToolSpec` defines the native schema, handler, timeout, recovery guidance, and optional action permission.
Tool calls are validated at both selection and execution time. Exact argument signatures prevent repeated calls
without blocking materially different follow-up research.

### Provider resilience

`OpenAIClient` supports OpenAI-compatible providers through explicit capability and error policies. It handles
token-field differences, native-tool incompatibility, fallback models, cooldown circuit breakers, transient
failures, and inline tool-call compatibility without treating every `403` as the same error.

### Observability

Each run receives a correlation ID. Nycti records ordered model, tool, and finalization steps with:

- requested and active models
- provider attempts and recovery paths
- prompt, completion, and total tokens
- tool argument hashes, status, latency, and provenance
- stop reason and end-to-end timing

`/logs` renders compact summaries, while per-message debug mode exposes the detailed agent trace.

### Evaluation

The test harness uses fake model turns and tool outcomes to replay direct answers, multi-tool flows, duplicate
calls, partial failures, empty responses, finalization, and continuation.

The built-in slash-command benchmarks exercise two different control-loop paths:

- `/benchmark earnings`: date-pinned, official-source NVIDIA-versus-AMD research and exact-value scoring
- `/benchmark context`: synthetic older-channel retrieval, decision tracking, ownership, open-question, and tool-policy scoring

Both report model/tool calls, token usage, latency, and deterministic quality checks.

This makes orchestration changes measurable instead of relying only on subjective chat quality.

## Capabilities

The current tool registry supports:

- Tavily web, image, and URL search/extraction
- Twelve Data quotes and price history, with Yahoo extended-hours fallback
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
- `src/nycti/chat/tool_eligibility.py`: deterministic tool selection
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
pip install -e .
python -m playwright install chromium
cp .env.example .env
python -m nycti.main
```

For Docker:

```bash
cp .env.example .env
docker compose up --build
```

Configuration is documented in [`.env.example`](.env.example). At minimum, set the Discord token, database URL,
chat provider credentials, and chat model. Tavily, Twelve Data, embeddings, vision, browser extraction, and the
debug channel are optional integrations.

## Useful Commands

- `/benchmark earnings`: evaluate the external research loop
- `/benchmark context`: evaluate older Discord-context retrieval and synthesis
- `/logs`: inspect model, token, tool, and timing summaries
- `/show debug:true`: attach the detailed trace to your replies
- `/memories` and `/memory`: inspect or manage selective memory
- `/reminders`: inspect pending reminders
- `/channel` and `/nickname`: manage server-specific aliases

See `/help` in Discord for the complete command list.
