# Changelog

## 2026-07-24

- improved memory attribution and recall: personal extraction now trusts only the caller's message, compact profiles
  preserve prior durable facts within a larger bound, explicit group conventions can become guild lore, and normal
  retrieval combines caller-private memories with labeled same-guild shared/lore matches from enabled users
- added an isolated synthetic-context benchmark for caller profiles, private memory, cross-user attribution, and
  shared guild lore so memory regressions are exercised through the normal agent prompt without production data
- improved short-callback routing by inheriting grounding hints from supplied recent context, preserving exact URLs
  for extraction, and limiting older-channel context to one bounded read per response
- deduplicated response feedback by the reviewed Nycti response and tightened self-report guidance so only the
  current request can flag the immediately previous answer
- strengthened response guidance for speaker attribution, unfamiliar services, provisional earnings reactions,
  focused ticker scope, and substantive reflection requests

## 2026-07-21

- changed Tavily's default search depth from `ultra-fast` to `basic` for more reliable grounding, while retaining
  explicit `fast`, `ultra-fast`, and `advanced` configuration overrides
- made live quote calls fall back to Yahoo's current regular-session snapshot when Twelve Data exhausts its
  per-minute credits, instead of discarding valid Yahoo prices unless pre-market or after-hours data exists
- added a `report_issue` observability tool so Nycti can archive its previous response when users describe a
  concrete mistake naturally, without requiring the exact `bad bot` phrase, then continue with the correction
- renamed the restricted calculation tool from provider-reserved `python` to `calc`; OpenAI had begun rejecting the
  entire native tool schema with HTTP 400, which silently forced live-market requests into a tools-disabled fallback
- added a Railway-friendly bad-bot feedback reader with concise and full output modes; its explicit `--clear` option
  removes only the rows displayed by that run so newly arriving feedback remains queued

## 2026-07-20

- made normal replies modestly shorter by default, with one to two sentences for casual asks and less repetition,
  generic caveating, and unnecessary follow-up offers
- improved ambiguous Discord callback handling and attribution by fetching bounded older context once when the
  supplied thread does not fit, asking a narrow clarification if needed, and treating human messages rather than
  Nycti's prior paraphrases as the source of what someone said
- made incomplete batched quote requests retry only failed symbols once when the user requested the full named set

## 2026-07-17

- stopped same-channel `send_msg` calls from creating confirmation proposals, directing the agent to answer and ping
  members in its normal reply instead, removed redundant model commentary from authoritative action cards, and
  excluded Nycti's own account from reply mention notifications
- retained observed Discord usernames, global names, and display names so natural in-channel requests can resolve
  members to exact mention tokens, and enabled user-only pings in fresh final messages while continuing to block
  role and everyone mentions
- made public-company quote results self-contained for valuation comparisons by processing Yahoo market cap and
  shares outstanding alongside live prices, and directed the agent to calculate price-to-match thresholds from
  those same-time inputs instead of searching intraday ranking headlines
- tightened current-state reasoning around intraday versus closing claims and guided member-reference questions to
  fetch bounded channel context when the supplied conversation window does not explain what changed
- added an optional country boost to general web search and local-language query guidance so requested regional
  research can prioritize local sources before Nycti translates and summarizes the evidence

## 2026-07-15

- added compact long-range extrema processing to `price_hist`: it can page through up to 20,000 daily candles,
  calculate historical highs/lows and the highest daily close server-side, report explicit coverage, and avoid
  sending raw history into the model for ATH and peak-drawdown questions
- strengthened the agent evidence contract for current market and sector explanations so it checks representative
  live breadth alongside current catalysts, and removed keyword promotion for personal-memory search to avoid
  confusing public domain terms such as memory stocks with stored Discord memory

## 2026-07-14

- fixed live foreign-exchange quotes by accepting canonical `BASE/QUOTE` pairs, normalizing common Yahoo `=X`
  aliases at the Twelve Data boundary, and guiding bare pair questions such as `what's USD/JPY?` to the quote tool

## 2026-07-12

- replaced misleading fixed response percentages with repeatable activity states, surfaced bounded tool-call names
  such as `web x2, quote`, and added elapsed time plus `/cancel` guidance for slow Discord requests

## 2026-07-11

- added durable UTC daily token reservations for the 1,000,000-token Terra allowance, with atomic concurrency-safe
  accounting and automatic Luna-high rollover before a request can exceed the remaining budget
- reduced unnecessary composite research by ordering promoted tool schemas first, moving unpromoted high-cost tools
  behind direct reads, documenting deep-research selection boundaries, and scoring direct benchmark cases against
  accidental `deep_research` calls
- added a checked-in Terra-versus-Luna high-reasoning timing comparison with per-case raw agent traces
- fixed the latest benchmark regressions by accepting candid missing-URL wording, making complete composite research
  terminal by default, routing discovered current listings directly to quotes, and stopping broad quote runs once
  they have enough coverage to synthesize
- kept configured high reasoning throughout Terra answer runs, including quick and post-tool synthesis turns, and
  made urllib-backed live providers portable across Python installs by augmenting platform trust with certifi

## 2026-07-10

- refreshed the checked-in benchmark result and raw trace snapshots from the latest 28-case GPT-5.6 Terra run,
  retaining exact failed/error answers, agent traces, and serialized step records for follow-up debugging
- fixed benchmark-derived scorer misses, deep-research input repair and first-party ranking, bounded evidence-ledger
  continuity, public-version routing, current-price quote flow, and one-turn quick-mode recovery capacity
- refreshed the checked-in 28-case GPT-5.6 Terra benchmark snapshot after the routing/evidence update, with exact
  failed checks, answers, agent traces, serialized steps, and separately labeled live-canary retry samples
- replaced the static delayed Discord status with one editable phase-based progress bar that follows context,
  model, tool, composition, and delivery milestones before becoming the final reply
- kept evidence provenance internal for normal Discord replies, removing automatic citation IDs, source lists, and
  citation-repair turns while preserving cited output for isolated live benchmarks
- updated response style guidance to avoid em dashes and canned "it's not X, it's Y" contrast phrasing
- addressed the nine observed live-benchmark failures by accepting equivalent numeric/unit morphology, replacing
  stale price ceilings with successful-quote coverage, tightening composite input contracts, discouraging redundant
  post-result lookups, batching quotes with a Yahoo fallback for primary-provider failures, and reserving enough time
  to synthesize after expensive research
- archived each explicitly submitted `bad bot` replay bundle in Postgres without expiry before attempting the
  debug-channel post, retaining full bounded diagnostics except credentials/tokens and logging archive/delivery status
- refreshed the checked-in live benchmark results and sanitized traces from the
  28-case GPT-5.6 Terra batch, including every failed output, tool path, and budget miss
- migrated the focused earnings, rich channel-context, SpaceX listing/price, and broad semiconductor quote
  benchmarks into the manifest-driven live suite, added distinct K-of-N regex scoring for sector coverage, and
  removed the parallel legacy benchmark runner and slash commands
- switched foreground inference to GPT-5.6 Terra with high reasoning and fixed blank
  `OPENAI_BASE_URL` values so the SDK uses the official endpoint instead of failing every primary request
- documented live earnings, context, SpaceX, and semiconductor benchmark results,
  including primary-provider fallback failures, latency, scorer limitations, and
  sanitized execution traces for future harness comparisons
- added an admin-run real-LLM evaluation suite spanning every read tool with short manifest prompts, frozen fixture
  and live-canary modes, deterministic success/grounding/speed scoring, repeat support, downloadable batch reports,
  isolated context, and 90-day redacted failure traces without contaminating member memory
- added configurable Discord invocation modes: compatible mention/reply behavior, leading explicit-name address,
  and allowlisted ambient channels with deterministic scope/rate gates, a bounded economy-model addressedness
  classifier, and per-user/channel cooldowns
- kept `bad bot` snapshots in memory by default; optional restart persistence now uses indexed, 15-minute expiring
  records, migrates still-valid legacy snapshots, and requires a direct reply to Nycti before reporting diagnostics
- routed composite deep-research planning and reduction directly to the configured cross-provider fallback model
  (DeepInfra DeepSeek V4 Pro in production), while retaining `OPENAI_EFFICIENCY_MODEL` when no fallback is set
- made stored memories private by default, added guild-bound `guild_shared` and `lore` scopes with requester-aware
  retrieval enforcement, an owner visibility command, and model-callable hybrid memory search
- replaced regex-derived action authorization with exact server-validated proposals, deterministic confirmation
  cards, requester/bot channel-permission checks, mention suppression, `/confirm`, and short-lived single-use
  capabilities; proposal cards no longer count as factual answer evidence
- kept every configured safe read tool directly reachable in every answer profile; routing signals now only promote
  likely tools, while quick/grounded/deep profiles control model, reasoning, token, turn, and time budgets
- made composite deep research a normal model-callable meta-tool that can combine economy-model web fan-out with
  bounded exact-URL extraction, live finance quotes, YouTube transcripts, and restricted calculations
- gave composite research weighted per-run and shared-concurrency limits, capability-gated every nested source,
  kept specialized evidence ahead of bounded web reductions, and prevented unavailable fallback tools from reappearing
- added direct/deferred exposure metadata, a labeled routing regression corpus, and runtime telemetry for exposure,
  unavailable promotions, meaningful call/grounding misses, latency, and citation-grounding quality
- added explicit quick-mode output caps for initial, post-tool, final, and continuation turns while preserving enough
  headroom for concise grounded answers
- fixed freshness-sensitive explanations such as newest-model, dated-policy, and named-version questions so all
  configured grounding tools remain available even when the request receives a fast budget
- restored one-pass hybrid memory retrieval when embeddings are configured, rejected memories without a minimum
  lexical or semantic signal, preserved separate private/guild candidate pools, normalized SQLite timestamps, and
  avoided embeddings for disabled or bot-related memory owners
- filled partial Discord cache windows from history with ID-based merging, and separated duplicate, quote, empty,
  and evidence-repair allowances under one global correction bound
- enforced requester visibility before reading linked Discord messages and rechecked private/locked/archived thread
  access before proposing or executing a confirmed channel send
- added bounded composite research: `OPENAI_EFFICIENCY_MODEL` plans two to four queries and reduces evidence while
  Tavily search and extraction run concurrently
- added deterministic quick, grounded, and deep answer profiles with `/depth` overrides, profile-specific
  deadlines/reasoning budgets, and larger hidden-reasoning output headroom for deep Responses calls
- added an ephemeral evidence ledger with stable IDs, bounded excerpts, citation/source guidance, one repair pass, deterministic source lists, and removal of invented URLs or unknown citations before delivery
- hardened stateless Responses reasoning continuity by requesting encrypted reasoning state, replaying complete output items across tools/corrections/continuations, and parsing refusals, incomplete reasons, cached input, and reasoning tokens
- moved agent-run telemetry commits behind a bounded background queue with shutdown draining, started request deadlines at Discord arrival, and added editable delayed progress plus user-scoped `/cancel`
- reduced foreground latency with budgeted answer profiles, one-pass hybrid memory retrieval when configured,
  Discord-cache-first context, and direct image input when the configured chat and vision model are the same
- enabled strict function schemas, canonical batched arguments, and historical-query-aware finance search windows while retaining legacy argument parsing for provider compatibility
- enforced the configured Discord guild for messages and application commands, made server administration checks fail closed, and disabled `plsfix` unless an exact administrator ID is configured
- blocked browser extraction from non-HTTP, credentialed, local, private, special-address, DNS-rebinding, redirect, and unsafe subresource destinations
- hardened Docker defaults with a secret-aware build context, non-root runtime, no-new-privileges, an internal-only PostgreSQL service, and a required deployment-supplied database password; moved Playwright to an optional browser extra
- switched primary inference from Clarifai Kimi K2.5 to OpenAI GPT-5.6 Luna with high foreground reasoning, minimal background reasoning, native tools, vision, accurate cost telemetry, and DeepInfra DeepSeek V4 Pro retained as model-call failover
- routed GPT-5.6 through OpenAI's Responses API with Responses-format tools, nested reasoning effort, stateless storage, image inputs, and function-call continuation while retaining Chat Completions for compatible backup providers
- added configurable foreground/background reasoning-effort settings
- fixed stale release/status reasoning by always including the user's local calendar date, requiring corrections to invalidate dependent conclusions, and explicitly rejecting past scheduled dates as still upcoming without current evidence
- added model-selectable web freshness/topic controls and surfaced source publication dates in Tavily evidence so current-status research can distinguish recent reporting from old announcements
- lowered post-tool answer temperature while preserving the more casual temperature for ordinary no-tool chat, improving factual synthesis without adding a planner or assessor call
- trimmed overlapping prompt wording so the stronger chronology and correction rules remain within the existing prompt-size budget

## 2026-07-09

- added `bad bot` response feedback capture: when said after a recent Nycti reply, it posts a redacted replay attachment with bounded Discord context, agent messages/tool results, exposed schemas, final reply, metrics, and correlated run telemetry to the configured debug channel
- removed the hidden `use search` and `fast search` command paths; users now ask naturally and the model chooses among broadly exposed read-only grounding tools
- restricted related-user memory lookup to structured Discord mentions and trusted member aliases instead of parsing user-controlled `user_id=` text
- restricted background memory/profile writes to the speaking user and made newly created memory settings opt-in by default
- replaced prose-keyword tool-result classification with typed execution status, metrics, provenance, and retryability, and count only successful outcomes as evidence
- moved restricted Python execution into an isolated subprocess with interpreter isolation, CPU/wall-clock limits, memory limits, bounded output, and asynchronous dispatch
- recorded each provider/model attempt, including native-tool mode, status, latency, and sanitized error details, while removing the memory model as a hidden foreground fallback
- persisted one correlated final run outcome with status and failure reason, and added debug-channel reporting when final synthesis falls back after a tool run
- expanded retention maintenance to prune agent runs, agent steps, tool events, and expired cross-channel action idempotency records
- split evaluation into deterministic earnings/context fixtures and changing SpaceX/semiconductor production canaries, with natural one-sentence benchmark prompts
- added GitHub Actions gates for tests, compilation, Ruff, and an initial typed-core mypy baseline; refreshed README, help, prompt, and generated prompt documentation
- removed speculative market-causality text from emergency web-result fallbacks so failed synthesis only surfaces evidence actually returned by tools

## 2026-06-16

- added final-answer failure diagnostics to latency debug logs so post-tool fallback replies show whether final synthesis timed out, provider-errored, returned empty text, or emitted raw output
- changed web-search failure fallback to return compact source-signal bullets with links instead of dumping unsynthesized Tavily snippets when final answer generation fails
- added `/benchmark semis`, a short-prompt sector quote benchmark for current semiconductor selloff questions that fails web-snippet fallbacks, missing MU, insufficient quote coverage, and obvious bad split-like prices
- disabled SDK-level retries for foreground agent chat turns so the per-request timeout cap actually leaves time for fallback instead of retrying the same stalled provider call
- capped foreground provider request timeouts inside the agent loop so a stalled primary model leaves time for cross-provider fallback and finalization instead of returning the generic clean-reply fallback
- changed `/benchmark earnings` to avoid forced `use search` behavior so benchmark prompts are generated once through the same ordinary chat path as user prompts
- strengthened current-price tool guidance, agent-loop correction, and `/benchmark spacex` scoring so a discovered public ticker must be verified with the market quote tool instead of web-only snippets
- changed prompt date/time context to a shorter local phrase with weekday/month words and no UTC offset
- tightened the SpaceX price benchmark scorer so market-cap or valuation-only answers no longer count as a current trading price
- shortened the SpaceX price and earnings benchmark prompts so they test Nycti's normal grounding behavior instead of embedding tool-use hints or fixture URLs
- added `/benchmark spacex`, an eval-style current price/status grounding benchmark that fails stale "SpaceX is private/no ticker" answers unless the run used web or quote tools
- removed the regex-forced web requirement for current-performance/status prompts and strengthened general prompt/tool guidance instead while keeping read-only tools broadly exposed
- added an admin-only `plsfix` mention shortcut that posts a recent diagnostic bundle to the configured error-debug channel as a text attachment for later debugging
- improved company-valuation searches by routing valuation and market-cap queries through fresher finance search and instructing Nycti to ignore unrelated crypto/token pages unless the user asks about tokens
- strengthened Nycti's broad grounding policy so volatile company-status questions such as IPOs, public/private status, ticker identity, market cap, and valuation are handled from current tool evidence instead of stale model memory
- expanded Nycti's system prompt to a medium-length agent prompt with clearer current-request priority, tool-grounding rules, stale memory handling, market freshness guidance, repeated-tool-loop control, and Discord formatting expectations
- rephrased the README so Nycti is framed first as a practical private Discord bot/tool while keeping the bounded agent-loop architecture documented as implementation detail

## 2026-06-14

- expanded the hybrid tool policy so every read-only tool is exposed on every turn without regex intent selection; reminders and cross-channel sends remain explicitly gated, and browser extraction is documented as a last resort
- switched tool exposure to a hybrid policy: web search, market quotes, and annual market performance are always available, while specialized tools remain intent-selected and action tools remain explicitly permission-gated
- added a deterministic Yahoo-backed `annual_perf` tool for calendar-year price changes and cash distributions, including batched JEPI/SPX comparisons and common `divident` phrasing; these requests must use the tool instead of model-memory estimates or invalid yield subtraction
- added an optional separately authenticated cross-provider chat fallback so foreground Clarifai failures can fail over to DeepInfra Kimi K2.5 without moving memory or normal traffic off Clarifai
- added one bounded backoff retry for foreground Clarifai model-busy responses while keeping background efficiency calls fail-fast, and removed unavailable dedicated-only fallback models from production configuration
- switched production inference back to Clarifai and configured Kimi K2.5 efficiency calls to use instant mode for memory extraction, profile updates, and bounded content summaries
- fixed newly listed ticker handling by requiring grounded market lookups, preferring current quote identity over stale model knowledge, routing ticker web searches through Tavily's fresh finance index, and using Yahoo's same-page regular close when Twelve Data conflicts with extended-hours data
- fixed startup changelog delivery for fresh or stale snapshots by splitting long announcements into Discord-safe messages and logging Discord status/code details when a fetch or send fails
- added `/benchmark context`, a deterministic synthetic Discord-history benchmark that verifies older-context tool use, final-decision tracking, task ownership, unresolved questions, deadline extraction, and avoidance of external research or superseded plans
- rewrote the README around the agent harness, bounded control loop, provider recovery, observability, and evaluation work while removing repeated feature and configuration detail
- removed test-only agent-eval and unused MCP adapter production modules, replaced their indirect coverage with direct tool-policy tests, removed the unreachable foreground profile-update tool and dead registry metadata, inlined one-use accounting, removed unused wrappers and helpers, and consolidated repeated elapsed-time calculations
- replaced the separate evidence, synthesis, rewrite, and forced-final branches with one typed bounded agent loop where the main model either calls another materially different tool or answers
- removed fuzzy web-query suppression and now block only exact normalized duplicate tool calls, allowing legitimate follow-up searches when initial evidence is incomplete
- added typed agent run, budget, stop-reason, permission, step, and tool-outcome contracts plus partial-success handling for concurrent tool execution
- added one empty-turn correction, one optional truncation continuation, an overall run deadline with finalization reserve, and one tools-disabled final pass on budget exhaustion
- improved Kimi inline-tool compatibility by inferring omitted tool names from distinctive argument shapes and defaulting ordinary unnamed URL calls to URL extraction
- added deterministic tool eligibility so common read tools stay available while URL, image, history, reminder, and cross-channel action tools are exposed only for matching request shapes
- added code-level rejection for tool calls that were not authorized for the current request
- narrowed cross-channel action eligibility to explicit send/post/announce language so ordinary “say” or “tell” requests cannot expose the channel-send tool
- deduplicated configured model fallbacks and added a cooldown circuit breaker for deterministic missing-model or missing-deployment failures so unavailable optional models are not called on every message
- replaced source-string orchestrator tests with deterministic model/tool replay tests for direct answers, follow-up tools, duplicate calls, empty turns, partial failures, finalization, and continuation
- consolidated tool schemas, permission flags, timeouts, fallback guidance, and handler bindings into one typed `ToolSpec` registry and replaced conditional executor dispatch with registered handlers
- enforced reminder and channel-send action permissions again at execution time and added source-message-based durable idempotency for cross-channel sends
- added explicit provider capability and error policies so tool incompatibility, authentication, deployment, access, rate-limit, and transient failures no longer share one 403 recovery path
- added buffered correlated agent-step telemetry with run IDs, ordered step indexes, requested/active models, provider attempts, tool argument hashes, statuses, stop reasons, latency, and token counts
- made tool outcomes carry direct latency, structured metrics, provenance URLs, and retryability in addition to status and content
- reduced prompt and retrieval work by exposing only request-relevant read tools, gating date/profile/memory context by relevance, removing duplicate tool instructions, and reusing one query embedding across caller and mentioned-user memory retrieval
- upgraded `/benchmark earnings` with a date-pinned official-source fixture, deterministic completeness and exact-value correctness scoring, missing/incorrect-field reporting, and model/tool/retry/token/latency metrics
- split inline/XML tool-call compatibility parsing out of the provider client and added module-size regression guards that keep the core orchestrator at or below 400 lines
- made provider-busy and rate-limit failures fail over to the next configured model with short cooldowns, record failed model steps in correlated telemetry, and finalize gracefully instead of surfacing uncaught reply errors
- buffered foreground model usage, tool events, and ordered agent-step telemetry into one end-of-run database transaction instead of committing after every model or tool step
- added separate output budgets for initial tool selection, post-tool replies, reserved final answers, and truncation continuation so reasoning-heavy models can emit grounded text without making every first turn expensive
- expressed `fast search` as a one-tool budget inside the normal loop instead of retaining a separate forced-final control branch
- normalized tool results at the executor boundary and added automatic optional-model suppression while a deployment/provider circuit breaker is active
- moved background memory/profile processing out of `bot.py` into a focused memory service and replaced its source-inspection test with behavior coverage
- moved startup changelog persistence and delivery out of `bot.py` into a focused service while preserving the slash-command-facing bot methods
- focused financial URL extraction on five query-ranked chunks for exact guidance, actual revenue, adjusted EPS, quarter, and report-date fields; classified empty extractions as typed empty outcomes with registry-owned recovery guidance; and verified the production benchmark at 10/10 completeness and 10/10 correctness against official NVIDIA and AMD investor-relations pages

## 2026-06-12

- added a short-timeout main-chat-model fallback when required post-tool synthesis cannot use the configured efficiency model, finalize after one evidence refinement instead of reopening another tool-capable round, and raised synthesis headroom so Kimi reasoning is less likely to trigger a separate continuation

## 2026-06-01

- made optional synthesis, memory extraction, and profile-update LLM calls fail fast without SDK retries and log transient provider-busy failures as warnings instead of noisy production errors
- changed failed web-search synthesis fallback to return compact source snippets and URLs instead of only asking the user to retry with a narrower question
- changed Yahoo quote-page extended-hours selection to infer the active session from the exchange-local clock before consulting Yahoo's sometimes stale `marketState`
- fixed Yahoo quote-page extended-hours parsing so postmarket requests use `postMarketPrice` when Yahoo reports `marketState=POST` instead of stale same-day `preMarketPrice`
- softened Nycti's prediction behavior so speculative "pick a date/number" follow-ups get a clearly labeled best-effort guess instead of a hard refusal when uncertainty is the only blocker
- fixed Yahoo extended-hours fallback so stale postmarket candles, such as Friday prints on Sunday night, are no longer shown as current after-hours stock moves when Yahoo omits or reports closed market state, and added a Yahoo quote-page scrape for live `overnightMarketPrice` before falling back to chart pre/post candles
- refocused the README around Nycti's agentic reply loop, including trigger gating, bounded context, tool execution, evidence synthesis, telemetry, and background memory
- added redundant web-query detection in the evidence loop so near-repeat search refinements are skipped, upgraded earnings/source-verification searches from Tavily `ultra-fast` to `basic`, strengthened earnings guidance toward official sources, and suppressed raw provider tool-call markup from final replies
- combined post-tool follow-up and synthesis into a compact evidence pass where the model can either answer from tool evidence or call another tool, rebuilding the prompt each round to avoid duplicated evidence history
- added an explicit `fast search`/`quick search` command that forces web search and finalizes after the first evidence-tool result to avoid an extra tool-capable refinement turn on latency-sensitive searches
- added `scripts/generate_example_prompt.py` and a regression test so `example_prompt.md` is generated from the real system prompt, user prompt builder, and native tool schemas
- stopped listing channel aliases in every prompt; Nycti now includes them only when the request looks like a cross-channel send/post request
- shortened Nycti's system prompt and repeated context/tool guidance while preserving the same Discord style, memory, date, and tool-use rules
- omitted placeholder-only prompt sections such as empty image, memory, alias, and extended-context blocks so ordinary replies send fewer repeated tokens
- removed per-message timestamps from default recent Discord context and filtered that default window to messages within 24 hours of the triggered message, while preserving timestamps for replies, links, anchors, and extended context
- shortened exposed native chat tool names such as `web`, `quote`, `channel_ctx`, and `yt_transcript` to reduce repeated tool-schema prompt tokens
- added `example_prompt.md` with a sanitized example of Nycti's full chat prompt/message payload and exposed native tools
- removed the LLM tool-planning prepass and now expose all chat tool schemas directly to the main reply model, eliminating `chat_tool_plan` latency while preserving tool availability
- added batched `web_search` queries so the model can request up to 4 independent Tavily searches in one tool call and Nycti runs them concurrently
- added configurable Tavily Search depth via `TAVILY_SEARCH_DEPTH`, defaulting to `ultra-fast` to reduce web-search tool latency while allowing `fast`, `basic`, or `advanced` overrides

## 2026-05-17

- removed redundant model/feature/tool section titles from compact debug log reports when the table header already names the section
- shortened `/logs` and daily debug summary display labels for common timing parts, features, models, and tools with tighter aliases like `e2e` and `clarif` so reports fit in Discord more reliably
- raised the `stock_quote` batch limit from 5 to 10 symbols across parser validation, native tool schema, planner metadata, and docs
- compacted `/logs` and daily debug summaries into lightly aligned code-block tables, trimming trailing padding and removing redundant model-feature/recent-tool sections so reports fit more reliably in one Discord message
- removed RSS/Atom feed polling and `/rss` slash commands from runtime, configuration, docs, and tests
- added a daily `last 24h` usage/timing summary posted to `ERROR_DEBUG_CHANNEL_ID` when the debug channel is configured
- made the Discord typing indicator fire once synchronously before context/model work starts, then continue as a background heartbeat
- fixed Discord typing indicators so the heartbeat starts before context collection and stays active through reply delivery, covering slow reply-chain/history fetches as well as model generation
- added Discord-safe formula handling so model replies that contain LaTeX display blocks are sent as code blocks instead of showing stray bracket delimiter lines
- added persisted per-message timing stats and `/logs` averages for debug parts such as context fetch, memory retrieval, model calls, tool phases, reply send, and end-to-end latency
- expanded chat-model failover detection to treat provider HTML 403/Forbidden/access-denied responses as fallback-worthy model/provider failures
- added a no-native-tools retry when an OpenAI-compatible provider rejects tool-bearing chat requests with 403/Forbidden, allowing plain replies to continue when fallback models are not configured
- restored the early-start Discord typing heartbeat after isolating the Clarifai 403s to provider/model behavior instead of typing indicators
- added detailed chat-provider attempt logs with requested/candidate models, provider URL, token-field variant, native-tool status, and sanitized provider error summaries
- added the configured efficiency model as a last-resort chat fallback when all explicit reply-model candidates are denied by the provider
- stripped appended tool-guidance prompt messages from no-native-tools chat retries so provider fallbacks are plain chat requests instead of still carrying tool-loop instructions
- added a compact plain-chat retry after stripped no-tool provider retries are still denied, preserving the current request and bounded recent context while dropping the heavier Discord prompt scaffold
- changed chat tool exposure to follow the planner's selected subset, remember provider native-tool rejection during a reply loop, and fall back to parseable XML tool calls without resending rejected native schemas
- added a latency-debug provider recovery notice when Nycti has to switch away from rejected native tool schemas during a reply
- added optional `ERROR_DEBUG_CHANNEL_ID` posting for compact operational debug messages when reply generation fails or provider/tool fallback recovery is used
- attached the full failed OpenAI-compatible request payload as JSON in error-debug posts so production provider failures can be reproduced without API keys
- reworded the system prompt's LaTeX formatting rule to avoid raw delimiter examples that Clarifai's gateway rejects with 403 on tool-bearing chat requests

## 2026-05-08

- removed stale unused imports, an obsolete private extended-context helper, and an unused YouTube URL wrapper/export after a repository dead-code pass
- split chat orchestrator support helpers into a separate module and added a regression test so tracked files stay at or below 1,000 lines
- changed tool-answer synthesis to derive its output budget from `MAX_COMPLETION_TOKENS` at one quarter of the effective chat reply cap instead of using a separate hardcoded cap
- made chat continuation robust to providers that omit `finish_reason=length`, raised the effective chat reply floor for existing low token-cap env values, and removed the 220-token synthesis bottleneck that could truncate tool-backed answers
- added a continuation pass when chat replies hit the model length limit and raised the default completion cap so table-heavy answers are less likely to be cut short
- fixed generated table PNGs so finance/math symbols and narrow spaces are normalized before rendering, avoiding missing-glyph artifacts in Discord table images
- made Discord typing indicators a safe best-effort heartbeat instead of a failing context manager so they stay visible during longer replies without letting typing 429s abort reply generation
- added warning logs and latency-debug fields for empty chat/final turns so generic clean-reply fallbacks expose which model phase returned no text
- clamped `MAX_COMPLETION_TOKENS` during env loading to the supported 64-8192 range with a warning so oversized deploy values do not crash startup

## 2026-04-28

- added an automatic Yahoo Finance extended-hours fallback for `stock_quote` when Twelve Data reports the regular market is closed, comparing the Yahoo pre/post-market price against the Twelve Data close
- split the chat tool executor into focused action, content, market, and telemetry modules so the main dispatcher stays under 1,000 lines
- made the Yahoo extended-hours fallback run outside normal U.S. trading hours even when Twelve Data omits `is_market_open`, and switched to Yahoo's `query2` chart host after `query1` returned rate-limit errors

## 2026-04-27

- added model-facing market guidance so comparisons between live data and historical benchmarks use tools for both sides instead of stale model-memory records
- removed the regex-based tool router from `orchestrator.py`, exposed the full native tool schema set consistently, simplified the planner away from `expose_tools`, and tightened prompt/tool guidance to use the authoritative current date and search-backed grounding for stale or historical facts

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
