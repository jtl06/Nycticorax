# AGENTS.md

This file is for AI coding agents working on `Nycti`.

## Project Summary

Nycti is a low-cost Discord AI bot for a private friend server.

Core product rules:
- Only respond when explicitly triggered (mention or reply to bot).
- Use short recent-channel context, not full history.
- Keep long-term memory selective, not exhaustive.
- Never store secrets, credentials, or sensitive data as memory.
- Optimize for low monthly cost and simple maintenance.

## Current Stack

- Python 3.11+, `discord.py`, OpenAI API, SQLAlchemy async ORM
- PostgreSQL in deployment, Docker / docker compose, `unittest`

## Key Files

- `src/nycti/main.py`: entrypoint.
- `src/nycti/bot.py`: Discord triggers, reminder delivery, changelog posting, reply dispatch.
- `src/nycti/chat/orchestrator.py`: tool loop and final-answer handling.
- `src/nycti/chat/context.py`: prompt/context preparation.
- `src/nycti/chat/tools/`: tool schemas, parsing, execution.
- `src/nycti/discord/`: slash-command modules.
- `src/nycti/config.py`: env loading and validation.
- `src/nycti/db/models.py`: SQLAlchemy models.
- `src/nycti/llm/client.py`: OpenAI wrapper.
- `src/nycti/memory/`: filtering, extraction, retrieval, service.

## Runtime Model

High-level flow:
1. A Discord message arrives.
2. `NyctiBot.on_message()` checks whether the message explicitly triggers the bot.
3. If triggered, the bot reads the current message plus a short recent channel window.
4. `ChatContextBuilder` prepares current date/time, channel aliases, and relevant memories in a short-lived DB session.
5. The chat model may call tools (web search, reminder creation, cross-channel post) before generating a reply.
6. Usage/cost is recorded without holding the same DB session open across the full tool loop.
7. A cheaper model decides in the background whether the prompt is worth saving as memory.
8. A background poller checks for due reminders and delivers them in-channel.

Integration notes:
- `use search` in a prompt forces at least one `web_search` tool call.
- Reminders are created via the tool loop, stored in DB, delivered by a background poller (~1/min).
- Cross-channel sends must be explicit and user-directed, limited to the current guild.
- `TAVILY_API_KEY` is optional at startup but required when the search tool is used.

## Non-Negotiable Constraints

- Never process every message with the LLM.
- Never store raw channel history as memory.
- Memory must clear both local heuristics and LLM judgment. Never store secrets, credentials, or sensitive data.
- Keep context windows small. Prefer cheaper models for memory extraction.
- Track approximate usage/cost for each LLM call.

If a proposed change weakens any of the above, call it out explicitly.

## Memory Contract

Good: preferences, recurring plans, ongoing projects, friend-server lore.
Bad: one-off jokes, short reactions, low-value chatter, secrets, credentials.

Implementation: local filtering in `memory/filtering.py`, category enforcement in `memory/extractor.py`, confidence gating via `MEMORY_CONFIDENCE_THRESHOLD`, lexical retrieval, summary-based dedupe.

If you change memory behavior: update tests, keep safety/cost posture intact.

## Database Notes

Current tables: `user_settings`, `memories`, `reminders`, `channel_aliases`, `app_state`, `usage_events`.

Tables are auto-created on startup. No migration framework yet — prefer backward-compatible schema changes or document the reset assumption.

## Configuration

Env config validated in `config.py`. See `.env.example` for all variables.

Rules:
- Keep defaults cheap and practical.
- Validate new env vars in `Settings`.
- Add new env vars to `.env.example` and `README.md`.

## Local Commands

```bash
pip install -e .                                    # setup
PYTHONPATH=src python3 -m pytest tests/             # tests
python -m nycti.main                                # run locally
docker compose up --build                           # run with Docker
```

## Coding Expectations

- Preserve modular structure. Keep core logic in services/orchestrators, not Discord handlers.
- Prefer small, composable services over giant handler methods.
- Add or update tests for behavior changes. Run the suite before committing.
- Follow existing style, use type hints, keep async boundaries correct.
- Keep replies within Discord message length limits.
- Avoid unnecessary dependencies.

## Git / Change Hygiene

- Do not commit `.env` or secrets.
- Update `src/nycti/changelog.md` for every user-facing change, operational change, bug fix, refactor, or new command before committing. Keep entries cumulative and dated.
- If you rename commands, models, env vars, or tables, update docs and tests in the same change.

## If You Are a Future Agent

1. Read `README.md`, then `bot.py`, then the relevant module.
2. Read existing tests before editing behavior.
3. Preserve the low-cost and selective-memory design unless explicitly asked to change it.
4. Add every meaningful change to `src/nycti/changelog.md` before committing.
