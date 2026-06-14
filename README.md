# Nycti

Nycti is a Discord AI agent for a private friend server. It only wakes up when explicitly triggered, builds a bounded view of the conversation, lets the chat model decide which tools to call, then turns tool evidence into a concise Discord reply.

## Agentic Reply Loop

Nycti's main behavior is an agent loop, not a one-shot chatbot call:

1. **Trigger gate**: Discord messages are ignored unless the bot is mentioned or someone replies to the bot. Slash commands are explicit utility flows.
2. **Bounded context build**: the bot collects the current prompt, a 24-hour recent channel window, reply-chain or linked-message context when relevant, image references, and relevance-gated memory/date blocks.
3. **Tool-capable chat turn**: the main chat model receives only the deterministically eligible native tool schemas. It can answer directly or call tools such as `web`, `url_extract`, `quote`, `channel_ctx`, `reminder`, or `send_msg`.
4. **Parallel tool execution**: independent tool calls run concurrently where possible. Web search supports batched parallel Tavily queries; market quotes can batch up to 10 symbols.
5. **Bounded agent loop**: tool results return to the same main model, which either calls a materially different tool or answers.
6. **Deterministic recovery**: exact duplicate calls are blocked, one empty-turn correction is allowed, and budget exhaustion gets one tools-disabled final pass.
7. **Telemetry and recovery**: one correlated run records ordered model/tool/final steps, tokens, latency, attempts, argument hashes, stop reasons, and provider recovery for `/logs` and debugging.
8. **Background memory**: after the reply is sent, a cheaper model decides whether the triggered interaction contains durable memory worth storing.

Cost and latency stay bounded because Nycti never runs the LLM for every server message, keeps default context small, fetches older channel history only through the `channel_ctx` tool, and uses cheaper models for memory extraction and summarization.

## Capabilities

- Responds only when:
  - the bot is pinged
  - someone replies to one of the bot's messages
- Runs explicit slash commands for utility flows
- Reads the current prompt plus a short recent channel window
- Fetches older channel context on demand through `channel_ctx`, either as a smaller raw window or a larger cheap-model summary
- Uses OpenAI-compatible models for main replies and cheaper background memory extraction
- Exposes only request-relevant native tool schemas to the main chat model and returns typed tool outcomes to the same bounded loop
- Relies on tool policy plus executor-side validation for safe use
- Defines schema, timeout, handler binding, fallback guidance, and action permission flags in one `ToolSpec` registry
- Stores only high-value memories above a confidence threshold
- Rejects secrets, credentials, and low-value chatter before storage
- Lets each user manage their own memories with slash commands
- Maintains a very short per-user profile note when memory is enabled and includes it as potentially stale background for that user's replies
- Can create reminders from normal chat requests and deliver them back in-channel
- Can fetch current market quotes through Twelve Data instead of relying on web search for live prices
- Can fetch recent historical market candles through Twelve Data for short price-history questions
- Can extract YouTube transcripts, summarize them with the efficiency model, and hand compact evidence to the main reply model
- Can use a Chromium-backed browser extraction tool for JS-heavy or blocked pages when basic URL extraction fails
- Can run a restricted Python calculation tool for math and small data transforms
- Sends markdown tables as PNG attachments in normal Discord replies so table layout survives Discord formatting
- Can optionally post a startup changelog into a configured Discord channel
- Can post into other channels through the chat tool loop when the bot has Discord permission and a channel alias or ID is provided
- Tracks token, tool-call, per-message timing, and correlated agent-step telemetry in PostgreSQL
- Renders compact agent traces in latency debug so model and tool time are visible

## Architecture Notes

- `discord.py` handles the trigger gate, slash commands, typing heartbeat, reply chunking, table images, and Discord permission boundaries.
- `ChatContextBuilder` prepares the bounded prompt context, including optional memory/profile/alias blocks only when useful.
- `ChatOrchestrator` owns one bounded loop: call the model, execute new tool calls, append typed outcomes, and finalize once when needed.
- `ChatToolExecutor` dispatches registered handlers for market data, Tavily, browser extraction, reminders, authorized channel sends, Python, YouTube transcripts, and older Discord context.
- `OpenAIClient` wraps OpenAI-compatible chat and embeddings using explicit provider capabilities, error classes, retry limits, fallback dedupe, and duration-based circuit breakers.
- `BackgroundMemoryWriter` keeps extraction/profile work outside the Discord runtime and skips optional models while provider cooldown is active.
- PostgreSQL stores user settings, memories, reminders, aliases, runtime app state, usage/tool events, correlated agent steps, and per-message debug timings. Foreground agent telemetry is flushed in one end-of-run transaction.
- Memory extraction is selective:
  - local heuristics reject obvious junk or sensitive text
  - a cheaper OpenAI model decides whether the message is worth remembering
  - profile updates are cooldown-gated to reduce churn
  - only confident, allowed categories are saved
- Memory retrieval is hybrid:
  - lexical ranking always works
  - if `OPENAI_EMBEDDING_MODEL` is configured, memories are also ranked semantically with stored embeddings
  - semantic and lexical relevance are blended with confidence, category, and recency
- Agent-tool behavior is covered by executable fake-model replays and direct eligibility-policy tests.
- Tool schemas, permission flags, timeouts, fallback guidance, and handler bindings share one registry in `src/nycti/chat/tools/registry.py`.
- Each user may also have a compact markdown profile note that the memory model updates from triggered interactions. It is capped and treated as possibly stale background, not truth.

## Slash Commands

- `/help page:<1-2>`: show commands, examples, and usage tips in short pages
- `/ping`: verify the bot is online and report gateway latency
- `/reminders`: show your pending reminders
- `/reminders_all`: show all pending reminders in this server (`Manage Server` required)
- `/forget_reminder reminder_id:<id>`: delete one of your pending reminders
- `/benchmark earnings`: score a date-pinned NVIDIA vs AMD earnings comparison for completeness, exact-value correctness, missing/incorrect fields, tool/model counts, tokens, retries, and latency
- `/config time timezone:<zone>`: set your timezone for reminders and date context
- `/show debug:<true|false> [memory:<true|false>] [thinking:<true|false>]`: toggle latency diagnostics, memory diagnostics, and/or reasoning summary visibility for your own replies
- `/test changelog`: post the current changelog message into the configured changelog channel (`Manage Server` required)
- `/cancel_all`: cancel all currently in-flight prompts (requires `Manage Server`)
- `/reset`: hard reset runtime state, cancel active prompts, clear runtime toggles, and refresh cached prompt state (requires `Manage Server`)
- `/logs [period:<day|week|custom>] [hours]`: show server usage and timing logs (`Manage Server` required)
- `/memories [userid:<id>]`: view your recent saved memories and IDs, or another user's if your account matches `DISCORD_ADMIN_USER_ID`
- `/memory enable:<true|false>`: enable or disable memory retrieval/storage for yourself
- `/memory forget:<id> [userid:<id>]`: delete one memory; `userid` is admin-only
- `/memory profile:<true> [userid:<id>]`: view the compact profile note; `userid` is admin-only
- `/memory clear_profile:<true> [userid:<id>]`: clear the compact profile note; `userid` is admin-only
- `/channel set alias:<name> channel_id:<id>`: create or update a channel alias (`Manage Server` required)
- `/channel delete alias:<name>`: delete a channel alias (`Manage Server` required)
- `/channel list`: list configured channel aliases
- `/nickname action:<add|delete|list> [user:<member>] [alias:<name-or-id>] [note:<text>]`: manage member aliases (`Manage Server` required for add/delete)

## Prompt / Tool Behavior

Triggers:
- mention the bot
- reply to one of the bot's messages
- use slash commands for explicit utility flows

Images:
- Nycti can include up to 3 image URLs from the current message, a short reply chain, same-server linked messages, and recent channel context.
- Included context images are labeled so the model can tell which message they came from.
- If `OPENAI_VISION_MODEL` is set, Nycti uses it for a separate image-summary prepass, then feeds that summary into the normal `OPENAI_CHAT_MODEL` tool/reasoning flow.
- For Clarifai-hosted Gemini vision models, Nycti downloads included images and sends them as base64 data URIs because those models cannot fetch external image URLs directly.
- If that separate vision prepass fails, Nycti falls back to sending the images directly to `OPENAI_CHAT_MODEL` when the base chat provider/model supports multimodal input.
- If `OPENAI_VISION_MODEL` is unset, Nycti falls back to `OPENAI_CHAT_MODEL` for direct multimodal requests.
- If `OPENAI_CHAT_MODEL_FALLBACKS` is set, Nycti will fail over to those backup chat models when the primary chat model starts returning model-level provider errors such as invalid-model, not-found, or forbidden responses. If no explicit fallback is available, Nycti can use the configured efficiency model as a last-resort plain-reply fallback.
- Non-image attachments still show up as attachment placeholders in recent context unless you add a dedicated file-reading tool later.

Search and extract:
- The model may use Twelve Data quotes for current market prices and daily change when configured, including up to 10 symbols in one quote request.
- The model may use Twelve Data price history for recent candles, prior closes, and short trend windows on one symbol.
- The model may use Tavily search when fresh web data helps, including batching up to 4 independent queries in one parallel tool call.
- Include `use search` to force at least one search call.
- Include `fast search` or `quick search` to force search and apply a one-tool budget before the normal tools-disabled final pass.
- The model may use Tavily image search for “what does this look like?” prompts and Tavily Extract for one exact URL.
- The model may use `yt_transcript` for YouTube video summaries, transcript questions, and focused questions about spoken video content.
- The model may use `browser_extract` (Chromium) for JavaScript-heavy pages or anti-bot-protected pages when normal extraction is insufficient.
- Examples:
  - `@Nycti use search latest NVDA earnings report`
  - `@Nycti fast search latest NVIDIA and AMD earnings`
  - `@Nycti what does a Cartier Tank look like?`
  - `@Nycti summarize this link: https://example.com/article`
  - `@Nycti summarize this YouTube video: https://youtu.be/dQw4w9WgXcQ`

Extended Discord context:
- Reply chains are included for triggered replies so the bot can see the replied-to post within the bounded reply depth.
- Recent channel context omits per-message timestamps and filters out messages older than 24 hours by default; reply chains, linked messages, and anchor context still include timestamp labels when needed.
- Nycti starts with the default small context, then may call `channel_ctx(mode, multiplier?, expand?)` if older Discord context is needed.
- `mode=raw` returns an older raw window of `5 * CHANNEL_CONTEXT_LIMIT * multiplier` messages.
- `mode=summary` fetches an older window of `25 * CHANNEL_CONTEXT_LIMIT * multiplier` messages and summarizes it with `OPENAI_EFFICIENCY_MODEL`.
- `multiplier` can be `1`, `2`, or `3`.
- Default context lines cap message text at `280` chars; set `expand=true` to use a wider `560` char cap.
- Nycti does not search arbitrary Discord channel history by keyword. It can read bounded recent/extended windows, reply chains, and exact same-server Discord message links.

Reminders and cross-channel actions:
- Reminders are stored in PostgreSQL, checked once per minute by default, and delivered in-channel with a ping and jump link when possible.
- Date-only reminders default to `09:00`.
- New users default to Pacific time (`America/Los_Angeles`); `/config time` overrides that per user.
- Configure channel aliases with `/channel set` before asking Nycti to post elsewhere.
- Channel aliases are only added to the model prompt when the request looks like a cross-channel send/post request.
- The bot still needs normal Discord send permissions in the target channel.

Member aliases:
- Use `/nickname action:add user:@friend alias:GTS note:"short context"` to teach Nycti server-specific nicknames.
- Aliases are stored in PostgreSQL and included as a small prompt block only when the alias appears in the current prompt or recent context.
- Keep notes short; this is for identity/context hints, not long biographies.

## Key Modules

- `src/nycti/main.py`: app entrypoint
- `src/nycti/bot.py`: Discord triggers, reply flow, and runtime glue
- `src/nycti/chat/`: prompt building, tool orchestration, and tool handlers
- `src/nycti/discord/`: slash-command registration and help text
- `src/nycti/twelvedata/`: Twelve Data market quote client and formatting
- `src/nycti/llm/client.py`: OpenAI-compatible client, provider fallbacks, and embedding paths
- `src/nycti/memory/`: memory extraction, filtering, retrieval, and persistence helpers
- `src/nycti/reminders/`: reminder parsing and delivery logic
- `src/nycti/tavily/`: Tavily search, image search, and extract integrations
- `src/nycti/browser/`: Chromium/Playwright extraction for blocked or JS-rendered pages
- `src/nycti/youtube/`: YouTube transcript extraction and formatting
- `src/nycti/db/`: SQLAlchemy models and session setup
- `tests/`: unit tests for config, LLM client, memory, reminders, Tavily, and helpers

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

```env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=123456789012345678
DISCORD_ADMIN_USER_ID=
ERROR_DEBUG_CHANNEL_ID=
OPENAI_API_KEY=sk-your-openai-key
OPENAI_BASE_URL=
TWELVE_DATA_API_KEY=
TWELVE_DATA_BASE_URL=https://api.twelvedata.com
DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/nycti
OPENAI_CHAT_MODEL=gpt-4.1-mini
OPENAI_CHAT_MODEL_FALLBACKS=
OPENAI_EFFICIENCY_MODEL=gpt-4.1-nano
OPENAI_MEMORY_MODEL=gpt-4.1-nano
OPENAI_VISION_MODEL=
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_EMBEDDING_API_KEY=
OPENAI_EMBEDDING_BASE_URL=
TAVILY_API_KEY=tvly-your-tavily-api-key
TAVILY_SEARCH_DEPTH=ultra-fast
MEMORY_CONFIDENCE_THRESHOLD=0.78
CHANNEL_CONTEXT_LIMIT=12
MEMORY_RETRIEVAL_LIMIT=4
MAX_COMPLETION_TOKENS=700
PROFILE_UPDATE_COOLDOWN_SECONDS=1800
REMINDER_POLL_SECONDS=60
BROWSER_TOOL_ENABLED=false
BROWSER_TOOL_TIMEOUT_SECONDS=20
BROWSER_TOOL_HEADLESS=true
BROWSER_TOOL_ALLOW_HEADED=false
PYTHON_TOOL_ENABLED=true
PYTHON_TOOL_TIMEOUT_SECONDS=3
PYTHON_TOOL_MAX_OUTPUT_CHARS=4000
YOUTUBE_TRANSCRIPT_ENABLED=true
YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS=10
YOUTUBE_TRANSCRIPT_MAX_CHARS=6000
```

## Local Run

1. Create the Discord bot in the Discord developer portal.
2. Enable the `MESSAGE CONTENT INTENT` for the bot.
3. Invite the bot with message, slash command, and send-message permissions.
4. Create `.env` from `.env.example`.
5. Install dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m playwright install chromium
```

6. Start PostgreSQL and run the bot:

```bash
python -m nycti.main
```

The app creates tables automatically on startup. If you use an OpenAI-compatible provider instead of OpenAI directly, set `OPENAI_BASE_URL` to that provider's API base URL and use the provider's model names.

`DISCORD_ADMIN_USER_ID` is optional. If set, that Discord user ID may run `/memories userid:<id>` to inspect another user's stored memories. Everyone else can only view their own memories.
Nycti also includes whether the current caller matches `DISCORD_ADMIN_USER_ID` in the prompt as owner/admin context.

`TWELVE_DATA_API_KEY` is optional until the bot uses the market-data tools. If it is unset, quote and price-history requests fail clearly.

`TWELVE_DATA_BASE_URL` defaults to `https://api.twelvedata.com`.

Twelve Data supports broader symbol coverage than the old Alpaca stock snapshot path, so `quote(symbol)` and `price_hist(symbol, ...)` can be used for supported stocks, ETFs, indexes, and some futures symbols. If Twelve Data says the regular market is closed, `quote` also tries Yahoo Finance chart data as a no-key pre/post-market fallback and compares that extended-hours price against the Twelve Data close. If a symbol is ambiguous or provider-specific, Nycti may return nearby symbol suggestions instead of a direct quote.

`OPENAI_CHAT_MODEL_FALLBACKS` is an optional comma-separated list of backup reply models. If the primary chat model starts returning deployment, access, transient, or rate-limit errors, Nycti temporarily marks it unhealthy and uses the next configured fallback instead of taking normal replies offline. Cooldowns are shorter for busy/rate-limited models and longer for missing deployments. If no explicit fallback is available, Nycti can use `OPENAI_EFFICIENCY_MODEL` as a last-resort reply model when it differs from the primary.

`OPENAI_EFFICIENCY_MODEL` is the cheaper model used for memory extraction, personal profile updates, and extended-context summaries. `OPENAI_MEMORY_MODEL` still works as a backward-compatible fallback if `OPENAI_EFFICIENCY_MODEL` is unset.

`PROFILE_UPDATE_COOLDOWN_SECONDS` sets the minimum gap between background profile updates per user (forced updates still run when new durable memory is stored).

`PYTHON_TOOL_ENABLED` controls the `python` tool. It defaults to `true` so Nycti can use bounded Python for math and small data transforms. The sandbox blocks imports, file access, private/dunder attributes, arbitrary builtins, and long-running code. Set it to `false` to disable local Python execution.

`PYTHON_TOOL_TIMEOUT_SECONDS` and `PYTHON_TOOL_MAX_OUTPUT_CHARS` cap execution time and returned output for `python`.

`YOUTUBE_TRANSCRIPT_ENABLED` controls the `yt_transcript` tool. It uses YouTube's public timed-text transcript endpoints without an API key, prefers English tracks when available, and summarizes capped transcript evidence with `OPENAI_EFFICIENCY_MODEL` before handing it to the main reply model. `YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS` and `YOUTUBE_TRANSCRIPT_MAX_CHARS` cap network wait time and transcript text sent into that summary step.

`OPENAI_EMBEDDING_MODEL` should be a normal OpenAI embedding model such as `text-embedding-3-small` or `text-embedding-3-large`.

`OPENAI_EMBEDDING_API_KEY` is optional. Set it when chat completions still use a different provider through `OPENAI_BASE_URL` but you want memory embeddings to go directly to OpenAI.

`OPENAI_EMBEDDING_BASE_URL` is optional. Set it when embeddings should use a different OpenAI-compatible endpoint than chat completions. If unset, embeddings use OpenAI's default base URL when `OPENAI_EMBEDDING_API_KEY` is set, or fall back to `OPENAI_BASE_URL` when they inherit the main API key.

`TAVILY_API_KEY` is optional until the bot attempts a web-search tool call, but Tavily requests will fail clearly if it is not set. `TAVILY_SEARCH_DEPTH` controls the search latency/relevance tradeoff and defaults to `ultra-fast`; supported values are `ultra-fast`, `fast`, `basic`, and `advanced`.

Browser extraction settings:
- `BROWSER_TOOL_ENABLED` defaults to `false`. Set it to `true` to allow Chromium page extraction.
- `BROWSER_TOOL_TIMEOUT_SECONDS` defaults to `20` and controls page-load timeout.
- `BROWSER_TOOL_HEADLESS` defaults to `true`.
- `BROWSER_TOOL_ALLOW_HEADED` defaults to `false`; set `true` only when you explicitly want headed Chromium sessions.

Nycti can call `channel_ctx` during the tool loop when older Discord context is needed. Raw context is smaller and goes directly to the main model; summary mode fetches more older messages and summarizes them with `OPENAI_EFFICIENCY_MODEL`.

Nycti can call `browser_extract(url, query?, headed?)` for JS-heavy/blocked pages. `url_extract` stays the default fast path and may fall back to browser extraction when Tavily extract fails.

Startup changelog:
- Set the server-side channel with `/config changelog`.
- Edit [`src/nycti/changelog.md`](src/nycti/changelog.md) before deploys when you want a custom changelog post.
- If `changelog.md` is empty or unavailable and `.git` is available, Nycti falls back to the latest local commit subject and short SHA.
- Nycti stores the last posted full changelog snapshot per server and only posts the newly added lines on later restarts.

`REMINDER_POLL_SECONDS` controls how often the bot checks for due reminders. `60` seconds is the default and is a reasonable tradeoff between responsiveness and overhead for a single private server.

Error debug posting:
- Set `ERROR_DEBUG_CHANNEL_ID` to a Discord channel ID to receive compact operational debug messages for hard reply-generation failures and recovered provider/tool fallback errors.
- The message includes IDs, model/tool metadata, and a sanitized error summary. It does not include raw prompt text or secrets.
- The same channel receives a daily `last 24h` usage/timing summary when configured.

## Docker Run

```bash
cp .env.example .env
docker compose up --build
```

The Docker image installs Playwright Chromium at build time so browser extraction works in deployment when `BROWSER_TOOL_ENABLED=true`.

`docker-compose.yml` starts:

- `db`: PostgreSQL 16
- `bot`: the Discord bot app

## Database Tables

- `user_settings`: one row per Discord user for memory on/off and timezone
- `memories`: distilled long-term memories only
- `reminders`: scheduled reminder deliveries
- `channel_aliases`: per-guild alias to channel-ID mapping
- `member_aliases`: server-managed member nicknames and short identity notes
- `app_state`: small persistent runtime state such as changelog channel config, daily debug summary state, and the last posted changelog snapshot
- `usage_events`: model usage telemetry per OpenAI-compatible call
- `tool_call_events`: tool-call status/latency telemetry for `/logs`
- `agent_step_events`: ordered per-run model/tool/finalization telemetry
- `message_debug_events`: per-message timing samples used for `/logs` latency averages
