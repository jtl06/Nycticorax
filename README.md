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
- `/ping`: verify the bot is online and report gateway latency
- `/memories`: view your recent saved memories and IDs
- `/forget memory_id:<id>`: delete one memory
- `/memory_on`: enable memory retrieval/storage for yourself
- `/memory_off`: disable memory retrieval/storage for yourself

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
│       ├── config.py
│       ├── main.py
│       ├── usage.py
│       ├── db
│       │   ├── models.py
│       │   └── session.py
│       ├── llm
│       │   └── client.py
│       └── memory
│           ├── extractor.py
│           ├── filtering.py
│           ├── retriever.py
│           └── service.py
└── tests
    ├── test_config.py
    └── test_memory_filtering.py
```

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

```env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=123456789012345678
OPENAI_API_KEY=sk-your-openai-key
OPENAI_BASE_URL=
DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/nycti
OPENAI_CHAT_MODEL=gpt-4.1-mini
OPENAI_MEMORY_MODEL=gpt-4.1-nano
MEMORY_CONFIDENCE_THRESHOLD=0.78
CHANNEL_CONTEXT_LIMIT=12
MEMORY_RETRIEVAL_LIMIT=4
MAX_COMPLETION_TOKENS=350
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
- `usage_events`: approximate usage/cost per OpenAI call

## Future MVP Extensions

- Add per-guild shared memories
- Add embeddings or PostgreSQL full-text search once memory volume justifies it
- Add retention cleanup for stale low-confidence memories
- Add moderation or allowed-channel controls
