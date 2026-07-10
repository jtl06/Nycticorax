# Nycticorax End-User Quality and Speed Review

Reviewed July 10, 2026 at commit `bbf0a79` after fast-forwarding local `main` to `origin/main`.

## Implementation Update — July 10, 2026

The working tree now implements the high-impact, self-contained parts of this review on top of the still-current
`origin/main` commit `bbf0a79`:

- quick, grounded, and deep answer profiles; focused grounded tool bundles; optional dedicated quick/deep models;
  `/depth mode:quick|grounded|deep|auto`; profile-specific reasoning, output, turn, and deadline budgets
- correct stateless Responses continuity using encrypted reasoning and complete output-item replay across tool,
  correction, and continuation turns; explicit refusal/incomplete/failure parsing and cached/reasoning/visible-token metrics
- a bounded evidence ledger with stable IDs, provenance-only URLs, citation auditing, one repair pass, deterministic
  source lists, and removal of invented links or unknown citations before delivery
- strict function schemas, canonical batched arguments with legacy parser compatibility, and historical-query-aware
  finance search windows
- bounded asynchronous telemetry, a deadline beginning at Discord event handling, cache-first Discord context,
  lexical-first memory retrieval, same-model direct vision, editable delayed progress, and user-scoped `/cancel`
- bounded composite research for eligible self-contained deep web-research questions: `OPENAI_EFFICIENCY_MODEL`
  plans two to four focused queries and reduces the evidence, Tavily search and extraction run concurrently, and
  the deep/foreground model performs one cited synthesis; specialized intents and total failure use the normal loop
- untrusted retrieved-content instructions in the system prompt, plus focused tests for evidence, prompt handling,
  routing, deadlines, cancellation, progress, and Responses state

The following remain separate product phases rather than silently expanding Nycti's authority or external surface:

- an optional isolated research critic/subagent for consequential conflicts; composite research itself is complete
- direct deterministic dispatch that bypasses the first model turn, final-answer streaming, HTTP/evidence caches,
  `/retry`, standalone `/sources`, and `/focus`
- PDF/document Q&A, private connectors, reviewed skill packages, and a broader evaluation corpus
- opt-in proactive briefs/monitors beyond existing reminders; these need explicit ownership, quiet hours, budgets,
  destinations, pause/expiry behavior, and a background-capacity lane

Verification after implementation: **498 tests passed**; Ruff, configured and changed-critical-file MyPy checks,
compileall, dependency checks, and `git diff --check` passed. The original review below remains a record of the
baseline findings at `bbf0a79`, so statements phrased as “currently” describe that baseline.

## Summary

The biggest improvement is not using a stronger model everywhere. Nycticorax should give different questions different execution paths:

```text
Request
├─ Quick: stable/simple → no tools → fast model
├─ Grounded: current/exact → relevant tool(s) → concise cited answer
└─ Deep: complex/verify → parallel research → evidence check → strong synthesis
```

The latest `main` already moves the baseline forward: it adds a Responses API adapter for OpenAI GPT-5.6 model names, separate foreground/efficiency reasoning settings, publication dates in web results, explicit search topic/time-window controls, and better freshness instructions. It also replaces the old “usually 1–3 sentences” constraint with “expand when useful.” The Responses path is only active when GPT-5.6 is explicitly configured; the clean `.env.example` still selects GPT-4.1 Mini. These are good foundations, but they do not yet choose quality, cost, tools, and latency per request or verify that cited evidence actually supports the answer.

## Immediate Issues in the New Responses Path

Fix these before relying on GPT-5.6 for Nycti’s multi-tool production path:

1. **Preserve reasoning state across tool turns.** The adapter uses `store: false`, does not request `reasoning.encrypted_content`, discards the response’s full output items, and reconstructs only chat-style text and function calls (`src/nycti/llm/responses_adapter.py:35`, `src/nycti/llm/responses_adapter.py:55`, and `src/nycti/chat/loop_messages.py:18`). OpenAI’s stateless guidance says to request encrypted reasoning content, preserve every output item, and replay the complete history. Keep those opaque items only for the active request; do not persist them in Nycti’s database. Add a real reasoning-item → tool-output → second-response contract test. [OpenAI reasoning-state guidance](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-without-stored-responses)
2. **Separate reasoning and visible-output budgets.** With the example’s 700-token setting, the loop allows 700 tokens initially, 1,400 after tools, and 2,000 for finalization, with a 15-second per-model-call cap (`src/nycti/chat/orchestrator_support.py:20` and `src/nycti/chat/model_runner.py:19`). Responses `max_output_tokens` includes hidden reasoning, so high effort can exhaust the limit before Nycti receives visible text. Use effort-aware budgets/deadlines, parse `incomplete_details.reason`, and record reasoning versus visible-output tokens. [OpenAI reasoning-token guidance](https://developers.openai.com/api/docs/guides/reasoning#allocating-space-for-reasoning)
3. **Do not mark failed responses healthy.** The client records an `ok` provider attempt and clears its cooldown before `parse_responses_turn()` can reject a failed response (`src/nycti/llm/client.py:709`). Parse inside the guarded attempt, then mark success; classify and fail over API-level failures, and handle refusal/incomplete output explicitly.

## Highest-Impact Changes

### 1. Add an adaptive answer router

Use deterministic signals—URLs, tickers, words such as “latest” and “verify,” comparisons, and calculations—rather than adding another planner-model call. Include a user override such as `/depth quick|auto|deep`.

Currently every request sees all ten read tools—roughly 7,000 characters of schemas and guidance—the same foreground model/reasoning profile, and the same 45-second agent-loop budget (`src/nycti/chat/tool_eligibility.py:28`, `src/nycti/chat/orchestrator.py:103`, and `src/nycti/chat/run_state.py:69`). Quick requests should see no tools; grounded and deep requests should see only relevant, enabled tools.

### 2. Make evidence a real contract

For researched answers, represent each source with an ID, URL, publisher, publication/retrieval date, excerpt, and source-quality tier. Require answers to cite those IDs and reject invented or unsupported links.

Deep mode should:

- Run two to four focused searches concurrently.
- Extract the best primary sources concurrently.
- Corroborate consequential claims.
- Label conflicts, inference, and unresolved uncertainty.

Current search now supplies publication dates when Tavily returns them and accepts explicit `topic` and `time_range` controls. However, it still gives the model only three 180-character snippets per query, has no typed evidence IDs or source-quality field, and accepts normal researched answers without citation validation. The default heuristic also applies a one-week filter to almost every stock-related query unless the model explicitly overrides it, so an omitted override can still damage historical research (`src/nycti/chat/tools/content.py:109`). Detect explicit years/historical wording in code and remove or widen that default.

The registered function tools are also non-strict: their schemas omit `additionalProperties: false`, several alternative fields are all optional, and the Responses adapter emits `strict: false` (`src/nycti/chat/tools/registry.py:32` and `src/nycti/llm/responses_adapter.py:197`). Normalize each tool to one unambiguous argument shape, make every property required where strict mode demands it, represent optional values with nullable types, and enable strict validation.

### 3. Remove hidden work from the reply path

The answer currently waits for telemetry serialization and a database commit (`src/nycti/chat/run_telemetry.py:130`). Move nonessential telemetry to a bounded background writer.

Also:

- Use Discord’s message cache before REST history calls.
- Use lexical memory retrieval first; embedding generation currently blocks the foreground path (`src/nycti/memory/service.py:215`).
- Apply one deadline beginning when the Discord message arrives.
- Send images directly to a vision-capable chat model instead of always performing a separate vision-summary call.
- Reuse asynchronous HTTP clients and add short, freshness-aware evidence caches.

### 4. Reduce model round trips

Known patterns can bypass the planning turn:

- Ticker price → fetch quote, then one synthesis.
- Explicit URL → extract immediately.
- Calculation → execute Python immediately.
- Reminder/action confirmation → deterministic formatting.
- Complex research → one composite research tool that searches, extracts, ranks, and returns normalized evidence.

### 5. Improve perceived speed and answer shape

The latest prompt correctly says to use one sentence for simple facts and expand when useful (`src/nycti/prompt.md:4`). Preserve that flexibility, but give deep answers a consistent shape:

- Conclusion
- Supporting evidence
- Material caveats
- Sources

Nycti already keeps Discord’s typing indicator alive. After roughly two seconds, upgrade that to one editable status such as “Checking sources…” with typed stages, then replace it with the answer. Stream only final synthesis at a rate-limited cadence. Add `/cancel`; ordinary users with an active request are currently directed toward the administrator-only `/cancel_all` command (`src/nycti/bot.py:414`).

### 6. Make quality versus speed measurable

Expand the four existing benchmarks into a replay corpus containing multi-source research, conflicting evidence, historical questions, calculations, ambiguous prompts, tool failures, prompt injection, images, and PDFs.

Gate releases on:

- Claim correctness and completeness
- Citation precision and support
- Unsupported-claim rate
- Appropriate abstention
- p50 and p95 latency by answer mode
- Model/tool calls; input, cached, reasoning, and visible-output tokens; incomplete-response rate; and cost

Production “bad bot” cases should become reviewed evaluation fixtures.

## Recommended Implementation Order

1. Harden Responses reasoning continuity, budgets, incomplete/failure handling, and deployment defaults.
2. Add adaptive routing, relevant-tool filtering, whole-request deadlines, and asynchronous telemetry.
3. Add structured evidence, citations, strict tool schemas, and corrected search heuristics.
4. Add progress delivery, cache-first context, model routing, and expanded evaluations.
5. Add PDF/text attachment Q&A with page-level citations.

Current `main` already routes explicitly configured OpenAI model names beginning with `gpt-5.6` through the Responses API and exposes foreground/efficiency reasoning effort. First harden the live adapter as described above. Then benchmark model/reasoning profiles for quick, grounded, and deep modes; replace the hard-coded model-name check with explicit provider capabilities; align the documented defaults with the intended deployment; measure prompt-cache hits; add final-answer streaming; and adopt strict tool schemas. Do not perform a blanket model replacement.

## Patterns Worth Borrowing From Other Agents

“Nous” most likely refers to [Nous Research’s Hermes Agent](https://github.com/NousResearch/hermes-agent), a general messaging agent that supports Discord and explicitly supports migration from OpenClaw. The recommendations below distinguish documented mechanisms from adaptations that would still need Nycti-specific evaluation.

### 1. Task profiles instead of one universal agent

Codex varies model and reasoning effort by task difficulty. OpenClaw exposes `/think` and `/fast`; Hermes exposes model, toolset, and context controls. Nycti now has configurable foreground and efficiency reasoning effort, but not per-request depth. Turn the proposed quick/grounded/deep routing into a first-class `AnswerPlan` containing:

- Model and reasoning effort
- Eligible tool bundle
- Deadline and output budget
- Maximum parallel workers
- Evidence and citation requirements
- Whether verification is required

Expose `/depth quick|auto|deep`, show the selected profile in `/status`, and allow `/retry deep` when a quick answer was insufficient.

### 2. A bounded research ensemble for deep questions

Codex, OpenClaw, Hermes, and Claude Code all use isolated workers for independent work. The valuable idea is context isolation, not “more agents” by itself: raw searches, extraction noise, and failed attempts stay outside the main synthesizer’s context.

For deep questions, use at most two to four read-only workers:

```text
Typed research plan
  ├─ source worker A
  ├─ source worker B
  └─ optional critic for conflicts/high-stakes claims
          ↓
Normalized evidence ledger
          ↓
One cited synthesis, with at most one repair pass
```

Workers should return structured evidence—claim, source URL, publisher, date, excerpt, confidence, and conflicts—not prose-only summaries. Use cheaper/faster models for source triage and the stronger model only for final synthesis. Never use this path for routine questions.

**Implementation status:** The bounded composite form is complete for eligible self-contained deep web research.
It uses `OPENAI_EFFICIENCY_MODEL` for two-to-four-query planning and evidence reduction, concurrent Tavily search
and extraction, and the deep/foreground model for one cited synthesis. Specialized tools remain on the normal path,
and a separate critic/subagent remains deferred.

### 3. Programmatic or composite tool pipelines

Hermes can run code that calls tools through RPC and reduces multi-step pipelines before returning results to the model. Nycti can adopt the simpler provider-independent version: a composite `research` tool that performs parallel search, ranking, extraction, deduplication, and evidence normalization in code.

This avoids repeated model turns for predictable work while preserving model judgment for query decomposition and final synthesis. Similar deterministic paths should cover quotes, calculations, URL extraction, reminders, and action confirmations.

**Implementation status:** The composite research pipeline is complete. Direct deterministic dispatch for the
other known patterns remains deferred.

### 4. Claims and evidence as typed state

OpenClaw’s optional memory-wiki represents claims with status, confidence, evidence, freshness, and contradiction reports. Nycti should first adapt that pattern as an ephemeral per-answer evidence ledger:

- Every consequential claim references known evidence IDs.
- Unsupported or conflicting claims are blocked or labeled.
- Citations can only render URLs present in tool provenance.
- Source freshness and quality are explicit fields.
- A deterministic validator runs before delivery; an LLM critic is reserved for consequential or conflicting answers.

This is more valuable for rigorous answers than adding unrestricted reflection loops.

### 5. Skills with controlled evolution

Codex, OpenClaw, and Hermes package repeatable workflows as skills. Nycti could add small declarative `SkillSpec` recipes for tasks such as earnings comparison, document Q&A, trip research, channel-decision summaries, and market-move analysis. Each recipe should declare triggers, tools, evidence policy, output contract, and budgets.

Hermes’ self-improvement loop is interesting, but production self-rewriting would create drift and prompt-poisoning risk. Prefer OpenClaw’s Skill Workshop pattern: feedback can generate a proposed prompt/skill/evaluation change, but an administrator reviews it and the candidate must beat the current version on the replay suite before adoption.

### 6. Session lanes, steering, and visible lifecycle

OpenClaw serializes work within a session while allowing bounded parallelism across sessions and separate interactive, subagent, and scheduled lanes. Hermes supports interrupt, queue, retry, undo, and editable progress.

Nycti should:

- Serialize by Discord thread/channel-user request key.
- Reserve provider capacity for interactive replies so background work cannot starve chat.
- Add `/stop`, `/retry`, `/sources`, and optionally `/focus <instruction>` for active deep runs.
- Immediately acknowledge accepted work and edit one status message through typed stages such as “searching,” “reading sources,” and “verifying.”
- Keep telemetry and persistence outside time-to-first-reply.

### 7. Health-aware, sticky failover

Nycti already has fallback and circuit-breaker logic. OpenClaw adds useful operational details: error-classified cooldowns, a temporarily sticky healthy fallback, periodic primary recovery probes, and one visible notice when fallback state changes.

Nycti should distinguish rate limits, overload, authentication, invalid requests, and timeouts; avoid retrying a known-unhealthy primary on every message; and never silently downgrade a user-selected deep-quality contract.

### 8. Progressive capability loading and trusted connectors

Codex uses skills and MCP connectors; OpenClaw and Hermes progressively load skills/toolsets. At Nycti’s current size, deterministic tool bundles are simpler than a tool-search subsystem. If the catalog grows, load compact capability descriptions first and full schemas only when selected.

Read-only MCP-style connectors could improve rigorous answers from private authoritative sources such as shared documents, calendars, project trackers, or server knowledge. Connector access should be scoped by guild/user, use least privilege, and identify private-source citations without leaking content to unauthorized users.

### 9. Opt-in proactive workflows

Responding only when triggered is not a hard product rule. OpenClaw and Hermes both support scheduled and event-driven work, so Nycti could add carefully bounded proactive features:

- Daily or weekly personalized briefs
- Upcoming-deadline and reminder follow-ups
- Monitors for explicitly selected companies, projects, topics, or Discord decisions
- “This changed since yesterday” alerts backed by fresh sources
- Scheduled channel summaries or unresolved-question digests

Every proactive workflow should require explicit creation, identify its owner and destination, support quiet hours and `/pause`, enforce daily token/tool budgets, deduplicate alerts, state why it fired, include sources, and expire or request renewal after inactivity. Background jobs should use a separate concurrency lane and never delay interactive replies.

### 10. Asynchronous observability and context hygiene

Codex batches telemetry asynchronously and flushes it on shutdown. Its subagent model also returns summaries instead of flooding the main context with raw logs. Nycti should use the same principles for its telemetry writer and evidence reducer.

Keep stable instructions and tool bundles at the front of prompts, volatile context at the end, cap raw tool output, and preserve original evidence outside the model prompt for citation validation and diagnostics.

## Patterns Not to Copy Blindly

- Do not run subagents or critics for ordinary questions.
- Do not permit live autonomous prompt, code, or skill rewriting.
- Do not install third-party skills without review, provenance, version pinning, and sandboxing.
- Do not persist raw Discord history merely to imitate long-lived agent sessions; retain Nycti’s selective-memory privacy contract.
- Do not build a full multi-channel gateway unless expansion beyond Discord is a real product goal.
- Do not use unlimited reflection or repair loops; cap verification at one repair attempt.

## Revised Priority Order

1. Harden Responses reasoning continuity, budgets, incomplete/failure handling, and deployment defaults.
2. Add relevant-tool selection, direct dispatch, whole-request deadlines, and asynchronous telemetry.
3. Add a structured evidence ledger, citation validation, and deterministic historical-query routing.
4. Add `/depth`, `/stop`, `/retry`, `/sources`, and editable progress.
5. **Complete:** Add composite parallel research. The optional deep-mode critic remains deferred.
6. Add approved skill proposals and evaluation-gated offline improvement.
7. Add opt-in proactive schedules/monitors with separate budgets and concurrency.
8. Add read-only private connectors where the friend server has authoritative shared data.

## Agent-System Sources

- [Codex manual: execution model, subagents, skills, security, and observability](https://developers.openai.com/codex/codex-manual)
- [OpenClaw repository](https://github.com/openclaw/openclaw)
- [OpenClaw Tool Search](https://docs.openclaw.ai/tools/tool-search)
- [OpenClaw subagents](https://docs.openclaw.ai/tools/subagents)
- [OpenClaw model failover](https://docs.openclaw.ai/concepts/model-failover)
- [OpenClaw skills and Skill Workshop](https://docs.openclaw.ai/tools/skills)
- [Hermes Agent repository](https://github.com/NousResearch/hermes-agent)
- [Hermes delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes self-evolution](https://github.com/NousResearch/hermes-agent-self-evolution)
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- [LangGraph workflow patterns](https://docs.langchain.com/oss/python/langgraph/workflows-agents)

## Relevant OpenAI Guidance

- [Current model and prompting guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Reasoning models and stateless reasoning continuity](https://developers.openai.com/api/docs/guides/reasoning)
- [Latency optimization](https://developers.openai.com/api/docs/guides/latency-optimization)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)

## Verification on Latest Main

- `pytest tests/ -q`: **414 passed**
- `ruff check src tests scripts`: **passed**
- Configured CI MyPy targets: **passed**
- `compileall`: **passed**
- `pip check`: **passed**
