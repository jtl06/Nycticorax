# Nycti

Nycti is a Discord AI bot for a private friend server. It answers questions, summarizes links and images, remembers a small amount of useful long-term context, and handles lightweight utility tasks like reminders, search, and channel posting.

## Features

- Responds only when:
  - the bot is pinged
  - someone replies to one of the bot's messages
- explicit slash commands
- Reads the current prompt plus the last 10-12 channel messages
- Can fetch older channel context on demand through a tool, either as a smaller raw window or a larger cheap-model summary
- Uses OpenAI-compatible models for main replies and cheaper memory extraction
- Uses an adaptive agentic tool flow: a cheaper model can plan whether tools are needed, then synthesize tool evidence into a concise final answer
- Exposes native tool schemas consistently to the main chat model and relies on tool policy plus executor-side validation for safe use
- Exposes tool metadata through a small registry and MCP-shaped descriptor adapter for future tool integrations
- Stores only high-value memories above a confidence threshold
- Rejects secrets, credentials, and low-value chatter before storage
- Lets each user manage their own memories with slash commands
- Maintains a very short per-user profile note when memory is enabled and includes it as potentially stale background for that user's replies
- Can create reminders from normal chat requests and deliver them back in-channel
- Can poll RSS/Atom feeds and post new items into a configured news channel without using the LLM
- Can fetch current market quotes through Twelve Data instead of relying on web search for live prices
- Can fetch recent historical market candles through Twelve Data for short price-history questions
- Can extract YouTube transcripts, summarize them with the efficiency model, and hand compact evidence to the main reply model
- Can use a Chromium-backed browser extraction tool for JS-heavy or blocked pages when basic URL extraction fails
- Can run a restricted Python calculation tool for math and small data transforms
- Sends markdown tables as PNG attachments in normal Discord replies so table layout survives Discord formatting
- Can optionally post a startup changelog into a configured Discord channel
- Can post into other channels through the chat tool loop when the bot has Discord permission and a channel alias or ID is provided
- Tracks token and tool-call usage telemetry in PostgreSQL
- Can render compact agent traces in latency debug so model, planner, tool, and synthesis time are visible

## Architecture Notes

- `discord.py` handles triggers and slash commands.
- PostgreSQL stores user settings, memories, reminders, channel aliases, app state, and usage/tool telemetry.
- Memory extraction is selective:
  - local heuristics reject obvious junk or sensitive text
  - a cheaper OpenAI model decides whether the message is worth remembering
  - profile updates are cooldown-gated to reduce churn
  - only confident, allowed categories are saved
- Memory retrieval is hybrid:
  - lexical ranking always works
  - if `OPENAI_EMBEDDING_MODEL` is configured, memories are also ranked semantically with stored embeddings
  - semantic and lexical relevance are blended with confidence, category, and recency
- Agent-tool behavior is covered by lightweight eval cases in `tests/agent_eval_cases.json`.
- Tool metadata lives in `src/nycti/chat/tools/registry.py`; MCP-style descriptors are exposed by `src/nycti/chat/tools/mcp_adapter.py`.
- Each user may also have a compact markdown profile note that the memory model updates from triggered interactions. It is capped and treated as possibly stale background, not truth.
- Cost stays low because:
  - no LLM call runs on every server message
  - context is capped
  - older channel history is only fetched on triggered requests when the model calls the context tool
  - memory extraction uses a cheaper model
  - memory retrieval still uses database + Python scoring for this scale

## Slash Commands

- `/help page:<1-2>`: show commands, examples, and usage tips in short pages
- `/ping`: verify the bot is online and report gateway latency
- `/reminders`: show your pending reminders
- `/reminders_all`: show all pending reminders in this server (`Manage Server` required)
- `/forget_reminder reminder_id:<id>`: delete one of your pending reminders
- `/benchmark earnings`: benchmark a no-context NVIDIA vs AMD earnings comparison and include latency output
- `/config time timezone:<zone>`: set your timezone for reminders and date context
- `/show debug:<true|false> [memory:<true|false>] [thinking:<true|false>]`: toggle latency diagnostics, memory diagnostics, and/or reasoning summary visibility for your own replies
- `/test changelog`: post the current changelog message into the configured changelog channel (`Manage Server` required)
- `/cancel_all`: cancel all currently in-flight prompts (requires `Manage Server`)
- `/reset`: hard reset runtime state, cancel active prompts, clear runtime toggles, and refresh cached prompt state (requires `Manage Server`)
- `/logs [period:<day|week|custom>] [hours]`: show server usage logs (`Manage Server` required)
- `/memories [userid:<id>]`: view your recent saved memories and IDs, or another user's if your account matches `DISCORD_ADMIN_USER_ID`
- `/memory enable:<true|false>`: enable or disable memory retrieval/storage for yourself
- `/memory forget:<id> [userid:<id>]`: delete one memory; `userid` is admin-only
- `/memory profile:<true> [userid:<id>]`: view the compact profile note; `userid` is admin-only
- `/memory clear_profile:<true> [userid:<id>]`: clear the compact profile note; `userid` is admin-only
- `/channel set alias:<name> channel_id:<id>`: create or update a channel alias (`Manage Server` required)
- `/channel delete alias:<name>`: delete a channel alias (`Manage Server` required)
- `/channel list`: list configured channel aliases
- `/rss add url:<feed> [channel:<channel>]`: add an RSS/Atom feed to post into a channel (`Manage Server` required)
- `/rss delete feed_id:<id>`: delete an RSS feed (`Manage Server` required)
- `/rss list`: list configured RSS feeds for this server
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
- If `OPENAI_CHAT_MODEL_FALLBACKS` is set, Nycti will fail over to those backup chat models when the primary chat model starts returning model-level provider errors such as invalid-model or not-found responses.
- Non-image attachments still show up as attachment placeholders in recent context unless you add a dedicated file-reading tool later.

Search and extract:
- The model may use Twelve Data quotes for current market prices and daily change when configured, including up to 5 symbols in one quote request.
- The model may use Twelve Data price history for recent candles, prior closes, and short trend windows on one symbol.
- The model may use Tavily search when fresh web data helps.
- Include `use search` to force at least one search call.
- The model may use Tavily image search for “what does this look like?” prompts and Tavily Extract for one exact URL.
- The model may use `youtube_transcript` for YouTube video summaries, transcript questions, and focused questions about spoken video content.
- The model may use `browser_extract_content` (Chromium) for JavaScript-heavy pages or anti-bot-protected pages when normal extraction is insufficient.
- Examples:
  - `@Nycti use search latest NVDA earnings report`
  - `@Nycti what does a Cartier Tank look like?`
  - `@Nycti summarize this link: https://example.com/article`
  - `@Nycti summarize this YouTube video: https://youtu.be/dQw4w9WgXcQ`

Extended Discord context:
- Reply chains are included for triggered replies so the bot can see the replied-to post within the bounded reply depth.
- Recent channel context includes timestamps.
- Nycti starts with the default small context, then may call `get_channel_context(mode, multiplier?, expand?)` if older Discord context is needed.
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
- `src/nycti/rss/`: RSS/Atom feed polling and post formatting
- `src/nycti/db/`: SQLAlchemy models and session setup
- `tests/`: unit tests for config, LLM client, memory, reminders, Tavily, and helpers

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

```env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=123456789012345678
DISCORD_ADMIN_USER_ID=
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
MEMORY_CONFIDENCE_THRESHOLD=0.78
CHANNEL_CONTEXT_LIMIT=12
MEMORY_RETRIEVAL_LIMIT=4
MAX_COMPLETION_TOKENS=350
TOOL_PLANNER_ENABLED=true
TOOL_ANSWER_REWRITE_ENABLED=true
TOOL_ANSWER_REWRITE_MIN_CHARS=260
PROFILE_UPDATE_COOLDOWN_SECONDS=1800
REMINDER_POLL_SECONDS=60
NEWS_CHANNEL_ID=
NEWS_RSS_URLS=
NEWS_POLL_SECONDS=300
NEWS_POST_LIMIT_PER_POLL=5
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

Twelve Data supports broader symbol coverage than the old Alpaca stock snapshot path, so `stock_quote(symbol)` and `price_history(symbol, ...)` can be used for supported stocks, ETFs, indexes, and some futures symbols. If a symbol is ambiguous or provider-specific, Nycti may return nearby symbol suggestions instead of a direct quote.

`OPENAI_CHAT_MODEL_FALLBACKS` is an optional comma-separated list of backup reply models. If the primary chat model starts returning model-level provider errors, Nycti temporarily marks it unhealthy and uses the next configured fallback instead of taking normal replies offline.

`OPENAI_EFFICIENCY_MODEL` is the cheaper model used for memory extraction, personal profile updates, and extended-context summaries. `OPENAI_MEMORY_MODEL` still works as a backward-compatible fallback if `OPENAI_EFFICIENCY_MODEL` is unset.

`TOOL_PLANNER_ENABLED` controls the cheap-model prepass that decides whether the reply should use tools, which tools are likely useful, whether freshness matters, and how risky stale information would be.

`TOOL_ANSWER_REWRITE_ENABLED` controls the adaptive second-pass synthesis for tool-heavy answers. When enabled, Nycti can run a short evidence-based synthesis pass on tool results and verbose tool-based drafts using `OPENAI_EFFICIENCY_MODEL`.

`TOOL_ANSWER_REWRITE_MIN_CHARS` sets the minimum draft length before synthesis triggers for non-evidence tool actions. Information tools such as search, URL extraction, market data, image search, and older channel context can synthesize even below this length.

`PROFILE_UPDATE_COOLDOWN_SECONDS` sets the minimum gap between background profile updates per user (forced updates still run when new durable memory is stored).

`PYTHON_TOOL_ENABLED` controls the `python_exec` tool. It defaults to `true` so Nycti can use bounded Python for math and small data transforms. The sandbox blocks imports, file access, private/dunder attributes, arbitrary builtins, and long-running code. Set it to `false` to disable local Python execution.

`PYTHON_TOOL_TIMEOUT_SECONDS` and `PYTHON_TOOL_MAX_OUTPUT_CHARS` cap execution time and returned output for `python_exec`.

`YOUTUBE_TRANSCRIPT_ENABLED` controls the `youtube_transcript` tool. It uses YouTube's public timed-text transcript endpoints without an API key, prefers English tracks when available, and summarizes capped transcript evidence with `OPENAI_EFFICIENCY_MODEL` before handing it to the main reply model. `YOUTUBE_TRANSCRIPT_TIMEOUT_SECONDS` and `YOUTUBE_TRANSCRIPT_MAX_CHARS` cap network wait time and transcript text sent into that summary step.

`OPENAI_EMBEDDING_MODEL` should be a normal OpenAI embedding model such as `text-embedding-3-small` or `text-embedding-3-large`.

`OPENAI_EMBEDDING_API_KEY` is optional. Set it when chat completions still use a different provider through `OPENAI_BASE_URL` but you want memory embeddings to go directly to OpenAI.

`OPENAI_EMBEDDING_BASE_URL` is optional. Set it when embeddings should use a different OpenAI-compatible endpoint than chat completions. If unset, embeddings use OpenAI's default base URL when `OPENAI_EMBEDDING_API_KEY` is set, or fall back to `OPENAI_BASE_URL` when they inherit the main API key.

`TAVILY_API_KEY` is optional until the bot attempts a web-search tool call, but Tavily requests will fail clearly if it is not set.

Browser extraction settings:
- `BROWSER_TOOL_ENABLED` defaults to `false`. Set it to `true` to allow Chromium page extraction.
- `BROWSER_TOOL_TIMEOUT_SECONDS` defaults to `20` and controls page-load timeout.
- `BROWSER_TOOL_HEADLESS` defaults to `true`.
- `BROWSER_TOOL_ALLOW_HEADED` defaults to `false`; set `true` only when you explicitly want headed Chromium sessions.

Nycti can call `get_channel_context` during the tool loop when older Discord context is needed. Raw context is smaller and goes directly to the main model; summary mode fetches more older messages and summarizes them with `OPENAI_EFFICIENCY_MODEL`.

Nycti can call `browser_extract_content(url, query?, headed?)` for JS-heavy/blocked pages. `extract_url_content` stays the default fast path and may fall back to browser extraction when Tavily extract fails.

Startup changelog:
- Set the server-side channel with `/config changelog`.
- Edit [`src/nycti/changelog.md`](src/nycti/changelog.md) before deploys when you want a custom changelog post.
- If `changelog.md` is empty or unavailable and `.git` is available, Nycti falls back to the latest local commit subject and short SHA.
- Nycti stores the last posted full changelog snapshot per server and only posts the newly added lines on later restarts.

`REMINDER_POLL_SECONDS` controls how often the bot checks for due reminders. `60` seconds is the default and is a reasonable tradeoff between responsiveness and overhead for a single private server.

RSS news posting:
- Add dynamic server feeds with `/rss add url:<feed> [channel:<channel>]`. These are stored in the database and can be removed with `/rss delete feed_id:<id>`.
- Set `NEWS_CHANNEL_ID` to a default Discord channel ID for `/rss add` when no `channel` is provided.
- Optionally set `NEWS_RSS_URLS` to one or more static comma-separated RSS/Atom feed URLs. `NEWS_RSS_URL` also works for a single static feed. Static env feeds require `NEWS_CHANNEL_ID`.
- `NEWS_POLL_SECONDS` defaults to `300`.
- `NEWS_POST_LIMIT_PER_POLL` defaults to `5` and is capped to prevent feed floods.
- On first startup for a feed, Nycti records existing feed items as seen and only posts newer items on later polls.

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
- `rss_feed_subscriptions`: server-managed RSS/Atom feeds added with `/rss`
- `member_aliases`: server-managed member nicknames and short identity notes
- `app_state`: small persistent runtime state such as changelog channel config, RSS seen-item IDs, and the last posted changelog snapshot
- `usage_events`: model usage telemetry per OpenAI-compatible call
- `tool_call_events`: tool-call status/latency telemetry for `/logs`
