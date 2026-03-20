# Nycti

Nycti is a low-cost Discord AI bot for a private friend server. It only calls the LLM when explicitly triggered, keeps a small rolling context window, and stores selective long-term memories instead of every message.

## Features

- Responds only when:
  - the bot is pinged
  - someone replies to one of the bot's messages
  - `/chat` is used
- Reads the current prompt plus the last 10-12 channel messages
- Uses OpenAI for:
  - main reply generation
  - cheaper memory extraction/classification
- Stores only high-value memories above a confidence threshold
- Rejects secrets, credentials, and low-value chatter before storage
- Retrieves a few relevant memories for future replies
- Lets each user manage their own memories with slash commands
- Can create reminders from normal chat requests and deliver them back in-channel
- Can optionally post a startup changelog into a configured Discord channel
- Can post into other channels through the chat tool loop when the bot has Discord permission and a channel alias or ID is provided
- Tracks approximate token usage and estimated cost in PostgreSQL

## Architecture Notes

- `discord.py` handles triggers and slash commands.
- PostgreSQL stores user memory settings, distilled memories, and usage events.
- Memory extraction is selective:
  - local heuristics reject obvious junk or sensitive text
  - a cheaper OpenAI model decides whether the message is worth remembering
  - only confident, allowed categories are saved
- Memory retrieval is lexical and local:
  - no embeddings required for the MVP
  - memories are ranked by overlap with the current prompt, confidence, category, and recency
- Cost stays low because:
  - no LLM call runs on every server message
  - context is capped
  - memory extraction uses a cheaper model
  - memory retrieval is database + Python scoring only

## Slash Commands

- `/chat prompt:<text>`: ask the bot something in-channel
- `/help`: show commands, examples, and usage tips
- `/ping`: verify the bot is online and report gateway latency
- `/reminders`: show your pending reminders
- `/reminders_all`: show all pending reminders in this server (`Manage Server` required)
- `/forget_reminder reminder_id:<id>`: delete one of your pending reminders
- `/benchmark earnings`: benchmark a no-context NVIDIA vs AMD earnings comparison and include latency output
- `/config time timezone:<zone>`: set your timezone for reminders and date context
- `/show debug enabled:<true|false>`: toggle latency diagnostics for your own replies
- `/show thinking enabled:<true|false>`: toggle concise reasoning summary visibility for your own replies
- `/test changelog`: post the current changelog message into the configured changelog channel (`Manage Server` required)
- `/cancel_all`: cancel all currently in-flight prompts (requires `Manage Server`)
- `/reset`: hard reset runtime state, cancel active prompts, clear runtime toggles, and refresh cached prompt state (requires `Manage Server`)
- `/memories`: view your recent saved memories and IDs
- `/forget memory_id:<id>`: delete one memory
- `/memory on`: enable memory retrieval/storage for yourself
- `/memory off`: disable memory retrieval/storage for yourself
- `/channel set alias:<name> channel_id:<id>`: create or update a channel alias (`Manage Server` required)
- `/channel delete alias:<name>`: delete a channel alias (`Manage Server` required)
- `/channel list`: list configured channel aliases

Web search trigger:
- The main chat model may call Tavily web-search tools even without `use search` when fresh web data would improve the answer.
- Include the exact phrase `use search` in a triggered prompt to force at least one web-search tool call before answering.
- The bot now nudges the model to prefer one strong search query before issuing follow-up searches.
- Example: `@Nycti use search latest NVDA earnings report`

Reminder behavior:
- The main chat model may call a reminder tool when you ask it to remind you later.
- Reminders are stored in PostgreSQL and checked once per minute by default.
- When due, Nycti posts in the same channel, pings the target user, and includes a jump link back to the original message when one exists.
- Date-only reminders default to `09:00` local bot time.
- New users default to Pacific time (`America/Los_Angeles`). `/config time` can override that per user.
- Example: `@Nycti remind me on 2026-03-25 to roll my NVDA calls`

Cross-channel posting:
- Configure aliases with `/channel set` so the bot has stable names like `alerts` or `ops`.
- The main chat model may call the channel-send tool only when you explicitly ask it to post somewhere else.
- The bot still needs normal Discord send permissions in the target channel.
- Example: `@Nycti post "deploy live" in alerts`

## Project Tree

```text
.
├── .env.example
├── Dockerfile
├── README.md
├── docker-compose.yml
├── pyproject.toml
├── src
│   └── nycti
│       ├── __init__.py
│       ├── bot.py
│       ├── changelog.py
│       ├── config.py
│       ├── main.py
│       ├── usage.py
│       ├── db
│       │   ├── models.py
│       │   └── session.py
│       ├── llm
│       │   └── client.py
│       ├── channel_aliases.py
│       ├── tavily
│       │   ├── client.py
│       │   ├── formatting.py
│       │   └── models.py
│       ├── reminders
│       │   ├── __init__.py
│       │   ├── parsing.py
│       │   └── service.py
│       └── memory
│           ├── extractor.py
│           ├── filtering.py
│           ├── retriever.py
│           └── service.py
└── tests
    ├── test_changelog.py
    ├── test_config.py
    ├── test_llm_client.py
    ├── test_reminders.py
    ├── test_tavily.py
    ├── test_memory_filtering.py
    └── test_timezones.py
```

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

```env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=123456789012345678
CHANGELOG_MESSAGE=
CHANGELOG_VERSION=
OPENAI_API_KEY=sk-your-openai-key
OPENAI_BASE_URL=
DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/nycti
OPENAI_CHAT_MODEL=gpt-4.1-mini
OPENAI_MEMORY_MODEL=gpt-4.1-nano
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

The app creates tables automatically on startup.

If you are using an OpenAI-compatible provider instead of OpenAI directly, set `OPENAI_BASE_URL` to that provider's API base URL and use the provider's model names.

`TAVILY_API_KEY` is optional until the bot attempts a web-search tool call, but Tavily requests will fail clearly if it is not set.

Optional startup changelog:
- Set the server-side channel with `/config changelog`.
- Set `CHANGELOG_MESSAGE` and `CHANGELOG_VERSION` during deploy for the most reliable changelog post.
- If those are unset and `.git` is available, Nycti falls back to the latest local commit subject and short SHA.
- Nycti stores the last posted changelog fingerprint and will not repost the same update on every restart.

`REMINDER_POLL_SECONDS` controls how often the bot checks for due reminders. `60` seconds is the default and is a reasonable tradeoff between responsiveness and overhead for a single private server.

## Docker Run

```bash
cp .env.example .env
docker compose up --build
```

`docker-compose.yml` starts:

- `db`: PostgreSQL 16
- `bot`: the Discord bot app

## Deploy Notes

- For a single private server, set `DISCORD_GUILD_ID` so slash commands sync faster.
- Start with a cheap chat model and a cheaper memory model.
- Review the `usage_events` table occasionally to understand token burn.
- If memory volume grows, add retention rules or cap memories per user.

## Database Tables

- `user_settings`: one row per Discord user for memory on/off
- `memories`: distilled long-term memories only
- `reminders`: scheduled reminder deliveries
- `app_state`: small persistent runtime state such as the last posted changelog fingerprint
- `usage_events`: approximate usage/cost per OpenAI call

## Future MVP Extensions

- Add per-guild shared memories
- Add embeddings or PostgreSQL full-text search once memory volume justifies it
- Add retention cleanup for stale low-confidence memories
- Add moderation or allowed-channel controls
