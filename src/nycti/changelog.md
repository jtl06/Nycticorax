# Changelog

## 2026-04-25

- added a `youtube_transcript` chat tool that extracts capped YouTube timed-text transcripts for video summaries and focused questions, with config caps for enablement, timeout, and summary input size
- updated tool planning, deterministic tool exposure, telemetry, docs, and tests so YouTube links prefer transcript extraction over generic page extraction when video content is needed
- changed `youtube_transcript` to summarize capped transcript evidence with the efficiency model before handing the result to the main reply model, preventing long raw transcript blocks from entering the main tool loop
- fixed provider inline tool-call leakage for `functions.tool_name:0` special-token markup by parsing that header shape and stripping known tool-call sections even when the tool is not currently exposed
- changed tool exposure so read-only information tools are always exposed through native tool schemas while write/action tools remain gated, and removed the stale prompt-level tool catalog that could encourage unavailable textual tool calls

## 2026-04-24

- changed dynamic tool exposure so the efficiency-model planner chooses the exact main-model tool subset via `expose_tools`, with deterministic logic kept as safety overrides for must-not-miss cases
- forced market-data tool use for live ticker/price-move prompts so short asks like "why is MU down today" cannot answer from stale model memory without a quote/search lookup
- fixed XML-style inline tool-call markup from OpenAI-compatible providers so snippets like `<function_calls>` are executed as tools instead of leaking into Discord replies
- improved generated table PNGs with larger fonts, wrapped cells, better column widths, and cleanup for markdown/citation artifacts
- added dynamic tool exposure so Nycti uses cheap planning and deterministic cues to send only likely-useful tool schemas to the main chat model, reducing repeated prompt-token overhead
- changed `python_exec` from admin-only opt-in to enabled-by-default restricted Python so Nycti can use it for math and small data transforms, while keeping sandbox, timeout, and output caps

## 2026-04-23

- added an optional admin-only `python_exec` tool with a restricted AST sandbox, timeout/output caps, and config flags (`PYTHON_TOOL_ENABLED`, `PYTHON_TOOL_TIMEOUT_SECONDS`, `PYTHON_TOOL_MAX_OUTPUT_CHARS`)
- changed normal Discord replies so markdown tables are rendered as PNG attachments instead of Discord-unfriendly markdown/code-block tables, with code-block fallback when no table image is generated
- expanded chat-model failover detection for Clarifai shared-compute/dedicated-nodepool model-routing errors
- added a cheap-model tool-planning prepass (`chat_tool_plan`) so Nycti can decide whether tools are needed, which tools to try, whether freshness matters, and how risky stale information would be before the main reply loop
- upgraded the tool-answer rewrite path into evidence-aware synthesis (`chat_reply_synthesis`) so information-tool results are converted into concise final answers instead of leaking raw search/extract output
- added `TOOL_PLANNER_ENABLED` and latency-debug fields for tool planning and tool-result synthesis
- added compact agent traces to latency debug so planner, chat, tool, synthesis, and forced-final stages can be profiled without new database tables
- added a tool metadata registry and MCP-shaped descriptor adapter so Nycti tools have explicit skill, cost, risk, env, permission, and fallback metadata
- added a lightweight agent eval case file and loader for regression checks around when tools should or should not be used
- added a repeated-tool-call breaker and clearer forced-final fallback so Nycti stops re-running identical tool calls and no longer reports a generic tool-call limit when no tools actually produced evidence

## 2026-04-22

- added Discord embed-preview context support so Nycti can read link-preview text (provider/title/description) from message embeds, including embed-only posts
- added a Chromium/Playwright browser-extraction tool (`browser_extract_content`) with optional headed mode flags so Nycti can read JS-heavy or blocked pages (for example PR Newswire) when basic extraction fails
- added an adaptive second-pass tool-answer rewrite flow (`chat_reply_rewrite`) so long tool-heavy drafts are compressed by the cheaper model before final reply delivery
- added profile update cooldown control (`PROFILE_UPDATE_COOLDOWN_SECONDS`) so background personal-profile refreshes are throttled unless a new durable memory was stored
- overlapped vision-prepass work with context preparation to reduce end-to-end latency on image-bearing requests

## 2026-04-17

- added persisted tool-call telemetry (`tool_call_events`) and a new `/logs` command to view recent usage by model, feature, and tool with token and estimated-cost rollups
- updated help text with `/logs` usage and scope permissions
- expanded `/logs` with period presets (`day`, `week`, `custom`) and model+category token breakdowns including a context-bandwidth summary
- simplified `/logs` to server-wide only (`Manage Server`), removed cost from the report, and compacted verbose Clarifai URL model IDs in output (for example `clarifai kimi-k2.5`)
- added automatic `usage_events` retention: rows older than 7 days are pruned at startup and then rechecked daily in the reminder background loop
- expanded automatic retention maintenance to also prune delivered reminders older than 30 days and stale memories (never retrieved for 90+ days or not retrieved for 180+ days)
- reduced `personal_profile_update` frequency by gating it to only run when a memory write occurred or the current message has durable-memory signal, lowering background memory-model churn
- added an explicit `update_personal_profile` chat tool path so Nycti can trigger a focused on-demand profile-note refresh when needed instead of relying only on background updates
- expanded background memory/profile updates so explicitly referenced users (mentions/aliases) can receive updates alongside the caller; saved profile lines now drop explicit mention/user-id references to reduce cross-user profile contamination
- capped per-message context text to 280 characters by default and added `get_channel_context.expand` so Nycti can request wider per-line context when exact longer wording is needed
- hardened tool-output handling so raw Tavily result blocks are no longer emitted verbatim as final replies; Nycti now gets an extra rewrite nudge and sanitized fallback behavior
- removed plain-word `nycti` triggering so replies now require an explicit mention or replying to Nycti
- replaced `/nickname add|delete|list` subcommands with a single `/nickname action:<add|delete|list>` command and shared optional parameters

## 2026-04-16

- prevented provider-side chat failures from crashing `on_message` by catching unexpected reply-generation exceptions and returning a short retry message instead
- expanded chat-model failover detection for provider-wrapped transient errors (for example Clarifai `model prediction failed` with `connection error` / `internal error`) so configured fallback chat models are used more reliably

## 2026-04-15

- fixed context assembly so replied-to and linked-message lines are pinned inside `CHANNEL_CONTEXT_LIMIT` instead of being dropped when recent channel history is full
- added bounded anchor-neighbor context so Nycti includes nearby before/after lines around replied or linked messages while still preserving recent-channel context

## 2026-04-13

- added selective memory retrieval for mentioned users and matched member aliases so Nycti can use another person's relevant memories when that person is directly referenced
- expanded non-bot Discord mention tokens in prompts and context into readable `@name (user_id=...)` labels so Nycti can tell who was pinged
- added `/nickname` commands and a `member_aliases` table so server-specific nicknames like `GTS` can be mapped to Discord users and selectively included in Nycti's prompt context when matched
- changed Nycti's persona prompt to avoid markdown tables and prefer Discord-friendly bullets or compact code blocks instead
- tightened Nycti's persona prompt to default to shorter, more direct replies and avoid over-explaining
- added `/rss add`, `/rss delete`, and `/rss list` so RSS/Atom feeds can be managed from Discord and stored in the database without redeploying
- added optional RSS/Atom news polling so Nycti can post new feed items into `NEWS_CHANNEL_ID` without using the LLM, with seen-item tracking to avoid startup floods

## 2026-04-12

- tightened channel-history summary behavior so Nycti avoids transcript dumps for chat-summary requests and will not fall back to dumping raw Discord context if final synthesis fails
- fixed embedding-backed memory retrieval crashing after successful embedding generation by importing the usage recorder used for embedding usage tracking
- compacted chat tool guidance and function descriptions to reduce repeated prompt tokens while keeping the same tool routing behavior
- extended `/memory` so the configured admin can view or clear another user's compact profile note and delete another user's memory by passing `userid:<id>`
- added `OPENAI_EFFICIENCY_MODEL` as a backward-compatible alias for the cheap model used by memory extraction, profile updates, and extended-context summaries, while keeping `OPENAI_MEMORY_MODEL` as a fallback
- added owner/admin prompt context from `DISCORD_ADMIN_USER_ID` and a compact per-user markdown profile note that the memory model can update in the background and include as possibly stale personal context on future triggered replies
- added an on-demand `get_channel_context` tool so Nycti can choose when to fetch older Discord context as either a smaller raw window or a larger `OPENAI_EFFICIENCY_MODEL` summary, with capped multipliers

## 2026-04-09

- made the injected current date/time context more explicit and told Nycti to treat it as authoritative for the current year and relative-date answers like today and tomorrow
- added a `price_history` market-data tool backed by Twelve Data so Nycti can fetch recent historical candles, prior closes, and short trend windows without falling back to web search
- expanded `stock_quote` so Nycti can request up to 5 market symbols in one tool call, fan them out into multiple Twelve Data quote requests internally, and show batch-aware market-data debug fields in latency debug
- updated the Twelve Data HTTP transport to send a normal browser-style user agent and collapse verbose Cloudflare/API error payloads into shorter surfaced error text
- tightened Twelve Data quote execution so provider error details are preserved, symbol-search fallback only runs for likely bad-symbol failures, per-symbol batch quotes run concurrently, and `stock_quote_count` remains a true tool-call count
- extended latency debug for `stock_quote` so Discord debug output now shows the market-data provider, requested symbol, quote status, and the surfaced Twelve Data error text when market-data requests fail
- added a plain-text `nycti` trigger so the bot can respond without a Discord ping when its name appears as a standalone word, and strip that trigger word from the prompt before reply generation
- shortened the emoji rule in `prompt.md` so it keeps the same behavior with less prompt clutter and less over-specific wording
- trimmed `prompt.md` further by collapsing repeated style/tool instructions, keeping the emoji meanings, and explicitly telling Nycti that memory may be outdated so it should prefer tools for fresh facts like prices, news, and specific pages
- loosened the prompt's table rule so Nycti may use compact tables when they help, since markdown tables are already normalized into Discord-friendly code blocks before sending

## 2026-04-08

- replaced the Alpaca market-data integration with Twelve Data so `stock_quote` can cover broader supported symbols like stocks, ETFs, indexes, and some futures, and now returns nearby symbol suggestions when a direct quote symbol is unsupported
- extracted Discord message-context collection helpers into `src/nycti/message_context.py` so `bot.py` keeps the higher-level runtime flow while reply-chain, linked-message, and image-context assembly live in a dedicated module
- extracted the vision prepass and data-URI image-preparation flow into `src/nycti/vision.py` so `bot.py` no longer owns low-level multimodal preprocessing and image-download logic

## 2026-04-04

- added an Alpaca-backed `stock_quote` chat tool so Nycti can fetch current U.S. stock snapshots directly instead of relying on web search for live price numbers
- tightened memory extraction to reject temporary shopping intent, promo/deal hunting, and one-off link-request state so long-term memory stays focused on durable preferences, projects, plans, and lore
- added optional `DISCORD_ADMIN_USER_ID` support so `/memories userid:<id>` can inspect another user's stored memories when explicitly allowed, while defaulting to your own memories when `userid` is omitted
- switched memory embeddings back to standard OpenAI embeddings and added optional `OPENAI_EMBEDDING_API_KEY` so embedding traffic can use a separate OpenAI key even when chat completions still point at another provider via `OPENAI_BASE_URL`
- removed the Clarifai-specific embedding request path and tightened blank embedding-input handling so memory retrieval/store calls do not send empty embedding payloads
- made vision-prepass failures log the configured vision model, image count, and exception text so image-analysis outages and provider mismatches are easier to diagnose from logs
- changed image routing so if `OPENAI_VISION_MODEL` fails, Nycti falls back to direct multimodal input on `OPENAI_CHAT_MODEL` instead of dropping image context entirely
- extended the memory debug block to show whether embeddings use a separate API key and whether they should target OpenAI directly or reuse the shared base URL, without exposing secrets
- added a Clarifai Gemini image-input workaround that downloads external image URLs and sends them as base64 data URIs for affected multimodal requests
- added optional `OPENAI_EMBEDDING_BASE_URL` support so embeddings can use a different OpenAI-compatible endpoint than chat completions

## 2026-03-20

- added `OPENAI_CHAT_MODEL_FALLBACKS` so reply generation can fail over to backup chat models when a provider model URL goes bad instead of taking the bot offline
- added support for direct Clarifai embedding `/outputs` endpoints so memory embeddings can use non-serverless Clarifai models like Qwen embedding deployments
- trimmed the README slightly to reduce repetition in the feature, image, and env sections
- replaced the full README project tree with a shorter key-modules overview
- rewrote the README intro blurb to describe Nycti's role and capabilities instead of implementation details
- folded memory enable/delete actions into a single `/memory` command with `enable` and `forget` options
- compressed `/help` from three pages to two while keeping each page within Discord's message limit
- tightened `prompt.md` slightly for shorter default replies and less redundant wording
- added Tavily Extract as a dedicated URL-content tool so the model can summarize specific pages without using search
- added multimodal image-attachment input for triggered messages when the configured chat model supports image input
- added optional `OPENAI_VISION_MODEL` routing so image-bearing requests can use a separate vision-capable model
- fixed image-bearing chat requests for OpenAI-compatible providers that require `max_completion_tokens` instead of `max_tokens`
- added fallback retries for providers that still reject image requests over token-field conflicts
- added optional `OPENAI_EMBEDDING_MODEL` support for hybrid semantic + lexical memory retrieval
- added bounded reply-chain and linked-message context so replies can include referenced text and images from same-guild Discord messages
- fixed `/benchmark earnings` after image-routing changes by passing the required empty image context for non-image benchmark runs
- made memory extraction more liberal by broadening durable-memory signals and adding a small confidence grace window for strong personal facts, goals, and routines
- added a separate per-user `/show memory` overlay so replies can show retrieved memories and memory state without enabling latency debug
- clarified the system prompt so Nycti explains memory correctly instead of claiming long-term memory is external or unavailable
- added Tavily-backed image search so Nycti can fetch direct image URLs for “what does this look like” requests and let Discord embed them
- added bounded startup retry/backoff for Discord 429 / Cloudflare 1015 login failures to avoid tight crash loops on temporary edge rate limits
- extended multimodal context so recent channel-context image attachments can also be passed to the vision model with source labels
- changed image handling so `OPENAI_VISION_MODEL` runs a separate image-summary prepass while the main chat/tool loop stays on `OPENAI_CHAT_MODEL`
- fixed Clarifai-backed embeddings by sending a minimal direct request to Clarifai's OpenAI-compatible `/embeddings` endpoint and retrying Clarifai model-name variants when the full model URL is rejected
- increased the chat tool-loop cap to 6 total rounds to allow slightly more search/extract iteration before forcing a final answer

## 2026-03-19

- extracted slash-command registration into `src/nycti/discord/*` modules
- extracted prompt/context building into `src/nycti/chat/context.py`
- extracted chat tool schemas, parsing, and execution into `src/nycti/chat/tools/`
- shortened DB session lifetime during chat replies so the full tool loop does not hold one open
- removed duplicate synthetic tool-result prompt messages during tool continuation

- simplified commands and moved help/changelog handling into dedicated modules
- added `/help` pages, channel aliases, and server-side changelog channel config
- added reminder listing/deletion plus startup changelog posting
- added reminders with per-user timezone config
- added token throughput to debug output
- improved search latency and Discord formatting behavior
- removed SEC integration and standardized on web search
- fixed inline tool traces and forced-final reply handling
- improved tool-driven search behavior and orchestration
- added chat-model tool orchestration with Tavily-backed search
- added custom emoji alias rendering
- hid `<think>` blocks unless debug is enabled
- added debug latency telemetry and reasoning summaries
- moved the system prompt into `prompt.md` and added cancel-all
- added `/ping`
- normalized Railway-style `DATABASE_URL` values
- added configurable OpenAI-compatible base URL support
- renamed the package from `Cinclus` to `Nycti`
- added agent/docs scaffolding
