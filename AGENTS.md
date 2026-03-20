# AGENTS.md

This file is for AI coding agents and other automated contributors working on `Nycti`.

## Project Summary

Nycti is a low-cost Discord AI bot for a private friend server.

Core product rules:
- Do not call the LLM on every server message.
- Only respond when explicitly triggered:
  - bot mention
  - reply to a bot message
  - `/chat`
- Use short recent-channel context, not full history.
- Keep long-term memory selective, not exhaustive.
- Never store secrets, credentials, or highly sensitive data as memory.
- Optimize for low monthly cost and simple maintenance.

## Current Stack

- Python 3.11+
- `discord.py`
- OpenAI API
- SQLAlchemy async ORM
- PostgreSQL in normal deployment
- Docker / docker compose
- `unittest` for the current test suite

## Repo Layout

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── src/nycti
│   ├── bot.py
│   ├── config.py
│   ├── main.py
│   ├── usage.py
│   ├── db/
│   ├── llm/
│   └── memory/
└── tests
```

Important files:
- `src/nycti/main.py`: app entrypoint.
- `src/nycti/bot.py`: Discord triggers, slash commands, reply generation.
- `src/nycti/config.py`: env loading and validation.
- `src/nycti/db/models.py`: SQLAlchemy models.
- `src/nycti/db/session.py`: async DB engine/session factory.
- `src/nycti/llm/client.py`: OpenAI wrapper and estimated cost calculation.
- `src/nycti/memory/filtering.py`: local heuristics for skip/sensitive checks and lexical retrieval scoring.
- `src/nycti/memory/extractor.py`: cheap-model memory extraction.
- `src/nycti/memory/retriever.py`: DB-backed memory ranking.
- `src/nycti/memory/service.py`: memory settings, CRUD, dedupe, retrieval, storage.
- `tests/test_config.py`: config validation tests.
- `tests/test_memory_filtering.py`: memory filter tests.

## Runtime Model

High-level flow:
1. A Discord message arrives.
2. `NyctiBot.on_message()` checks whether the message explicitly triggers the bot.
3. If triggered, the bot reads the current message plus a short recent channel window.
4. The bot retrieves a few relevant stored memories for that user.
5. The main chat model may call tools such as web search, reminder creation, or cross-channel posting before generating a reply.
6. Usage/cost is recorded.
7. A cheaper model may decide whether the current prompt is worth saving as memory.
8. If valid and above threshold, a distilled memory is stored.
9. A background poller checks for due reminders and delivers them in-channel.

Slash commands currently implemented:
- `/chat`
- `/help`
- `/ping`
- `/reminders`
- `/reminders_all`
- `/forget_reminder`
- `/benchmark earnings`
- `/config time`
- `/show debug`
- `/show thinking`
- `/test changelog`
- `/cancel_all`
- `/reset`
- `/memories`
- `/forget`
- `/memory on`
- `/memory off`
- `/channel set`
- `/channel delete`
- `/channel list`

Tavily integration notes:
- Use `src/nycti/tavily/` for Tavily client and formatting helpers.
- The main chat model may call the Tavily search tool when fresh web data would materially improve the answer.
- If the exact phrase `use search` appears in a triggered prompt, the tool must be called before the final answer.
- The main chat model may call the Tavily search tool multiple times before producing the final answer.
- Prefer one strong search query before firing multiple searches in sequence.

Reminder integration notes:
- Use `src/nycti/reminders/service.py` for reminder scheduling and due-reminder queries.
- Reminders are created through the main chat tool loop, not from every message.
- Reminders are stored in the `reminders` table and checked by a background polling task.
- Due reminders should ping the target user in-channel and include a jump link back to the source message when available.
- Keep reminder polling cheap; the default cadence is once per minute.
- Require `TAVILY_API_KEY` for requests and fail clearly if it is missing.
- Keep result formatting concise and include source URLs.

Channel alias / cross-channel posting notes:
- Use `src/nycti/channel_aliases.py` for alias normalization and DB lookups.
- Keep cross-channel sends explicit and user-directed; do not let the bot spray messages across channels speculatively.
- Prefer configured aliases over raw IDs in prompts and tool calls.
- Channel sends should be limited to channels inside the current guild.

## Non-Negotiable Product Constraints

Do not change these casually:
- Never process every message with the LLM.
- Never store raw full-channel history as long-term memory.
- Never store memory unless it clears both:
  - local safety/value heuristics
  - LLM-based memory judgment
- Never store:
  - passwords
  - API keys
  - tokens
  - SSNs
  - financial data
  - similarly sensitive content
- Keep context windows small by default.
- Prefer cheaper models for memory extraction/classification than for chat replies.
- Track approximate usage/cost for each OpenAI call.

If a proposed change weakens any of the above, call it out explicitly.

## Memory Contract

Good memory categories:
- preferences
- recurring plans
- ongoing projects
- useful friend-server lore

Bad memory candidates:
- one-off jokes
- short reactions
- low-value chatter
- secrets
- credentials
- highly sensitive personal data

Current implementation details:
- Local filtering lives in `src/nycti/memory/filtering.py`.
- Allowed categories are enforced in `src/nycti/memory/extractor.py`.
- Confidence gating is controlled by `MEMORY_CONFIDENCE_THRESHOLD`.
- Retrieval is lexical, not embedding-based.
- Duplicate handling is simple summary-based dedupe.

If you change memory behavior:
- update tests
- keep the safety and cost posture intact
- update `README.md` if behavior changes materially

## Database Notes

Current tables:
- `user_settings`
- `memories`
- `usage_events`

Notes:
- Tables are created automatically on startup.
- There is no migration framework yet.
- Be careful with schema changes because fresh environments are easy, but upgrades are not yet formalized.

If adding schema changes, prefer one of these:
- keep changes backward-compatible
- add a lightweight migration story
- document the reset/rebuild assumption clearly

## Configuration

Env config is validated in `src/nycti/config.py`.

Important environment variables:
- `DISCORD_TOKEN`
- `DISCORD_GUILD_ID`
- `CHANGELOG_MESSAGE`
- `CHANGELOG_VERSION`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `TAVILY_API_KEY`
- `DATABASE_URL`
- `OPENAI_CHAT_MODEL`
- `OPENAI_MEMORY_MODEL`
- `MEMORY_CONFIDENCE_THRESHOLD`
- `CHANNEL_CONTEXT_LIMIT`
- `MEMORY_RETRIEVAL_LIMIT`
- `MAX_COMPLETION_TOKENS`

Rules:
- Keep defaults cheap and practical.
- Validate new env vars in `Settings`.
- Add new env vars to `.env.example`.
- Document new env vars in `README.md`.
- `TAVILY_API_KEY` should be treated as optional at startup but required whenever the Tavily tool is used.

## Local Commands

Setup:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Compile/import sanity:

```bash
PYTHONPATH=src python3 -m compileall src tests
```

Run locally:

```bash
python -m nycti.main
```

Run with Docker:

```bash
docker compose up --build
```

## Coding Expectations

When making changes:
- Preserve the current modular structure unless there is a strong reason to refactor.
- Keep business rules out of Discord event handlers when possible.
- Prefer small, composable services over giant handler methods.
- Add or update tests for behavior changes.
- Keep replies within Discord message length limits.
- Keep code ASCII unless there is a clear reason not to.

For Python:
- Follow the existing style in the repo.
- Use type hints where practical.
- Keep async boundaries correct.
- Avoid unnecessary dependencies.

## Testing Expectations

Minimum expectation for non-trivial changes:
- add or update unit tests
- run the current test suite

If you cannot run tests:
- say so clearly
- explain why
- describe the unverified risk

## Common Safe Extensions

Reasonable next steps:
- better memory ranking
- per-guild shared memories
- retention policies for stale memories
- admin controls for allowed channels
- improved observability around usage/cost
- a migration framework

Higher-risk changes that need more care:
- embeddings/vector search
- storing more raw user content
- automatic memory extraction outside explicit triggers
- broader autonomous bot behavior

## Git / Change Hygiene

- Do not commit `.env`.
- Do not add secrets to the repo.
- Keep commits focused.
- If you change user-facing behavior, update `README.md`.
- If you rename commands, models, env vars, or tables, update docs and tests in the same change.

## If You Are a Future Agent

Start here:
1. Read `README.md`.
2. Read `src/nycti/bot.py`.
3. Read the relevant module for the area you are changing.
4. Read the existing tests before editing behavior.
5. Preserve the low-cost and selective-memory design unless explicitly asked to change it.
