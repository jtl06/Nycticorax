# Nycti

Nycti is a Discord AI bot for a private friend server. It answers questions, summarizes links and images, remembers a small amount of useful long-term context, and handles lightweight utility tasks like reminders, search, and channel posting.

## Features

- Responds only when:
  - the bot is pinged
  - someone replies to one of the bot's messages
- explicit slash commands
- Reads the current prompt plus the last 10-12 channel messages
- Uses OpenAI-compatible models for main replies and cheaper memory extraction
- Stores only high-value memories above a confidence threshold
- Rejects secrets, credentials, and low-value chatter before storage
- Lets each user manage their own memories with slash commands
- Can create reminders from normal chat requests and deliver them back in-channel
- Can fetch current market quotes through Twelve Data instead of relying on web search for live prices
- Can optionally post a startup changelog into a configured Discord channel
- Can post into other channels through the chat tool loop when the bot has Discord permission and a channel alias or ID is provided
- Tracks approximate token usage and estimated cost in PostgreSQL

## Architecture Notes

- `discord.py` handles triggers and slash commands.
- PostgreSQL stores user settings, memories, reminders, channel aliases, app state, and usage.
- Memory extraction is selective:
  - local heuristics reject obvious junk or sensitive text
  - a cheaper OpenAI model decides whether the message is worth remembering
  - only confident, allowed categories are saved
- Memory retrieval is hybrid:
  - lexical ranking always works
  - if `OPENAI_EMBEDDING_MODEL` is configured, memories are also ranked semantically with stored embeddings
  - semantic and lexical relevance are blended with confidence, category, and recency
- Cost stays low because:
  - no LLM call runs on every server message
  - context is capped
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
- `/memories [userid:<id>]`: view your recent saved memories and IDs, or another user's if your account matches `DISCORD_ADMIN_USER_ID`
- `/memory enable:<true|false>`: enable or disable memory retrieval/storage for yourself
- `/memory forget:<id>`: delete one memory
- `/channel set alias:<name> channel_id:<id>`: create or update a channel alias (`Manage Server` required)
- `/channel delete alias:<name>`: delete a channel alias (`Manage Server` required)
- `/channel list`: list configured channel aliases

## Prompt / Tool Behavior

Triggers:
- mention the bot
- say `nycti` in a message
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
- The model may use Tavily search when fresh web data helps.
- Include `use search` to force at least one search call.
- The model may use Tavily image search for “what does this look like?” prompts and Tavily Extract for one exact URL.
- Examples:
  - `@Nycti use search latest NVDA earnings report`
  - `@Nycti what does a Cartier Tank look like?`
  - `@Nycti summarize this link: https://example.com/article`

Reminders and cross-channel actions:
- Reminders are stored in PostgreSQL, checked once per minute by default, and delivered in-channel with a ping and jump link when possible.
- Date-only reminders default to `09:00`.
- New users default to Pacific time (`America/Los_Angeles`); `/config time` overrides that per user.
- Configure channel aliases with `/channel set` before asking Nycti to post elsewhere.
- The bot still needs normal Discord send permissions in the target channel.

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
REMINDER_POLL_SECONDS=60
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
```

6. Start PostgreSQL and run the bot:

```bash
python -m nycti.main
```

The app creates tables automatically on startup. If you use an OpenAI-compatible provider instead of OpenAI directly, set `OPENAI_BASE_URL` to that provider's API base URL and use the provider's model names.

`DISCORD_ADMIN_USER_ID` is optional. If set, that Discord user ID may run `/memories userid:<id>` to inspect another user's stored memories. Everyone else can only view their own memories.

`TWELVE_DATA_API_KEY` is optional until the bot uses the stock-quote tool. If it is unset, market quote requests fail clearly.

`TWELVE_DATA_BASE_URL` defaults to `https://api.twelvedata.com`.

Twelve Data supports broader symbol coverage than the old Alpaca stock snapshot path, so `stock_quote(symbol)` can be used for supported stocks, ETFs, indexes, and some futures symbols. If a symbol is ambiguous or provider-specific, Nycti may return nearby symbol suggestions instead of a direct quote.

`OPENAI_CHAT_MODEL_FALLBACKS` is an optional comma-separated list of backup reply models. If the primary chat model starts returning model-level provider errors, Nycti temporarily marks it unhealthy and uses the next configured fallback instead of taking normal replies offline.

`OPENAI_EMBEDDING_MODEL` should be a normal OpenAI embedding model such as `text-embedding-3-small` or `text-embedding-3-large`.

`OPENAI_EMBEDDING_API_KEY` is optional. Set it when chat completions still use a different provider through `OPENAI_BASE_URL` but you want memory embeddings to go directly to OpenAI.

`OPENAI_EMBEDDING_BASE_URL` is optional. Set it when embeddings should use a different OpenAI-compatible endpoint than chat completions. If unset, embeddings use OpenAI's default base URL when `OPENAI_EMBEDDING_API_KEY` is set, or fall back to `OPENAI_BASE_URL` when they inherit the main API key.

`TAVILY_API_KEY` is optional until the bot attempts a web-search tool call, but Tavily requests will fail clearly if it is not set.

Startup changelog:
- Set the server-side channel with `/config changelog`.
- Edit [changelog.md](/Users/jacenli/Documents/Discord%20bot/src/nycti/changelog.md) before deploys when you want a custom changelog post.
- If `changelog.md` is empty or unavailable and `.git` is available, Nycti falls back to the latest local commit subject and short SHA.
- Nycti stores the last posted full changelog snapshot per server and only posts the newly added lines on later restarts.

`REMINDER_POLL_SECONDS` controls how often the bot checks for due reminders. `60` seconds is the default and is a reasonable tradeoff between responsiveness and overhead for a single private server.

## Docker Run

```bash
cp .env.example .env
docker compose up --build
```

`docker-compose.yml` starts:

- `db`: PostgreSQL 16
- `bot`: the Discord bot app

## Database Tables

- `user_settings`: one row per Discord user for memory on/off and timezone
- `memories`: distilled long-term memories only
- `reminders`: scheduled reminder deliveries
- `channel_aliases`: per-guild alias to channel-ID mapping
- `app_state`: small persistent runtime state such as changelog channel config and the last posted changelog snapshot
- `usage_events`: approximate usage/cost per OpenAI call
