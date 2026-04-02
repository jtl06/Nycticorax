# Nycti

Nycti is a low-cost Discord AI bot for a private friend server. It only calls the LLM when explicitly triggered, keeps a small rolling context window, and stores selective long-term memories instead of every message.

## Features

- Responds only when:
  - the bot is pinged
  - someone replies to one of the bot's messages
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
- `/memories`: view your recent saved memories and IDs
- `/memory enable:<true|false>`: enable or disable memory retrieval/storage for yourself
- `/memory forget:<id>`: delete one memory
- `/channel set alias:<name> channel_id:<id>`: create or update a channel alias (`Manage Server` required)
- `/channel delete alias:<name>`: delete a channel alias (`Manage Server` required)
- `/channel list`: list configured channel aliases

Trigger model:
- mention the bot in a message
- reply to one of the bot's messages
- use slash commands for the explicit utility flows above

Image attachments:
- If a triggered message includes image attachments, Nycti now passes up to 3 image URLs with the main prompt as multimodal input.
- If the triggered message replies to another message, Nycti also includes a short bounded reply chain plus any image attachments from those replied-to messages.
- If the triggered message or reply chain includes Discord message links from the same server, Nycti also fetches those linked messages and their image attachments when available.
- If a recent message in the normal channel context has an image attachment, Nycti can now include that image too, with a short label telling the model which context message it came from.
- If `OPENAI_VISION_MODEL` is set, Nycti uses it for a separate image-summary prepass, then feeds that summary into the normal `OPENAI_CHAT_MODEL` tool/reasoning flow.
- If `OPENAI_VISION_MODEL` is unset, Nycti falls back to `OPENAI_CHAT_MODEL` for direct multimodal requests.
- If `OPENAI_CHAT_MODEL_FALLBACKS` is set, Nycti will fail over to those backup chat models when the primary chat model starts returning model-level provider errors such as invalid-model or not-found responses.
- Non-image attachments still show up as attachment placeholders in recent context unless you add a dedicated file-reading tool later.

Web search trigger:
- The main chat model may call Tavily web-search tools even without `use search` when fresh web data would improve the answer.
- Include the exact phrase `use search` in a triggered prompt to force at least one web-search tool call before answering.
- The bot now nudges the model to prefer one strong search query before issuing follow-up searches.
- Example: `@Nycti use search latest NVDA earnings report`

Image search:
- The main chat model may call Tavily image search when you ask what something looks like or explicitly want an example image.
- The tool returns direct image URLs, and Nycti can include one in the reply so Discord embeds it inline.
- Example: `@Nycti what does a Cartier Tank look like?`

URL extraction:
- The main chat model may call Tavily Extract when you give it one exact URL and ask for a summary or question-specific answer.
- This is separate from search: use extraction when the page is already known, and search when the bot needs to find sources first.
- Example: `@Nycti summarize this link: https://example.com/article`

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
│       ├── changelog.md
│       ├── changelog.py
│       ├── chat
│       │   ├── __init__.py
│       │   └── orchestrator.py
│       ├── config.py
│       ├── main.py
│       ├── usage.py
│       ├── discord
│       │   ├── __init__.py
│       │   └── help.py
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
OPENAI_API_KEY=sk-your-openai-key
OPENAI_BASE_URL=
DATABASE_URL=postgresql+psycopg://postgres:postgres@db:5432/nycti
OPENAI_CHAT_MODEL=gpt-4.1-mini
OPENAI_CHAT_MODEL_FALLBACKS=
OPENAI_MEMORY_MODEL=gpt-4.1-nano
OPENAI_VISION_MODEL=
OPENAI_EMBEDDING_MODEL=
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

`OPENAI_CHAT_MODEL_FALLBACKS` is an optional comma-separated list of backup reply models. If the primary chat model starts returning model-level provider errors, Nycti temporarily marks it unhealthy and uses the next configured fallback instead of taking normal replies offline.

`OPENAI_EMBEDDING_MODEL` may be either:
- a normal embedding model name / OpenAI-compatible model URL
- a direct Clarifai `/outputs` endpoint URL for embedding models such as Qwen embedding deployments

For direct Clarifai `/outputs` embedding URLs, Nycti sends a native Clarifai REST request with `Authorization: Key ...` and reads `outputs[0].data.embeddings[0].vector`.

`TAVILY_API_KEY` is optional until the bot attempts a web-search tool call, but Tavily requests will fail clearly if it is not set.

Optional startup changelog:
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

## Deploy Notes

- For a single private server, set `DISCORD_GUILD_ID` so slash commands sync faster.
- Start with a cheap chat model and a cheaper memory model.
- Review the `usage_events` table occasionally to understand token burn.
- If memory volume grows, add retention rules or cap memories per user.

## Database Tables

- `user_settings`: one row per Discord user for memory on/off and timezone
- `memories`: distilled long-term memories only
- `reminders`: scheduled reminder deliveries
- `channel_aliases`: per-guild alias to channel-ID mapping
- `app_state`: small persistent runtime state such as changelog channel config and the last posted changelog snapshot
- `usage_events`: approximate usage/cost per OpenAI call

## Future MVP Extensions

- Add per-guild shared memories
- Add embeddings or PostgreSQL full-text search once memory volume justifies it
- Add retention cleanup for stale low-confidence memories
- Add moderation or allowed-channel controls
