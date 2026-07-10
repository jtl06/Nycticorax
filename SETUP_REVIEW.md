# Nycticorax Repository Setup Review

Reviewed July 10, 2026 at commit `bbf0a79` after fast-forwarding local `main` to `origin/main`.

## Implementation Update — July 10, 2026

The working tree now addresses the highest-risk setup findings on top of the current `origin/main` commit
`5924647`:

- configured-guild enforcement now covers message handling and application-command dispatch
- guild administration checks fail closed for DMs and unknown user types; server administration commands are
  guild-only; `plsfix` is disabled without an exact configured administrator ID
- Chromium extraction allows only validated public HTTP(S) destinations, rejects credentialed/local/private/special
  addresses and alternate numeric forms, pins DNS to a validated public address, blocks cross-host redirects,
  subresources, WebSockets, proxies, and unsafe browser background networking, and revalidates the final URL
- `.dockerignore` excludes secrets and local state; the bot image runs as a non-root user with no-new-privileges;
  PostgreSQL is internal-only and requires a deployment-supplied password
- Playwright is now an optional `browser` extra; the Docker image installs it explicitly, while minimal local
  installations avoid the Chromium dependency
- local contributor instructions install the development extra, and optional Tavily configuration is empty by default

Still deferred because they require migration/deployment policy rather than a safe local patch: Alembic and a tested
PostgreSQL upgrade path, a reviewed hash-locked dependency workflow, project-wide MyPy cleanup, immutable image/action
pins, a license choice, and broader CI security/coverage gates.

Verification after the latest implementation: **630 tests passed**; Ruff, configured and changed-critical-file
MyPy checks, compileall, dependency checks, and `git diff --check` passed. Docker was still unavailable for an image build, so
container changes have static regression tests; browser network isolation has focused mocked launch/routing tests.
The original review below remains the baseline record at `bbf0a79`.

## Summary

Nycticorax is well structured, clearly documented, and has a substantial passing test suite. The most important issues are authorization boundaries and arbitrary browser navigation. The browser issue is conditional because browser extraction is disabled by default.

## Priority Findings

### 1. Authorization can fail open

The bot processes triggered messages from any guild (`src/nycti/bot.py:396`), while `DISCORD_GUILD_ID` only scopes slash-command registration and synchronization (`src/nycti/bot.py:174` and `src/nycti/bot.py:620`). If the bot is invited to another server, it can therefore process requests there and use the same API credentials and database.

`can_manage_guild()` also returns `True` for any user object that is not a `discord.Member` (`src/nycti/discord/common.py:12`). Administrative commands such as `/cancel_all` and `/reset` only verify that a user exists before calling this helper (`src/nycti/discord/core.py:89`). This could fail open in global-command or direct-message contexts.

The `plsfix` telemetry command is restricted only when `DISCORD_ADMIN_USER_ID` is configured (`src/nycti/bot.py:569`). With the example's blank default, any server user can invoke it.

Recommended changes:

- Enforce the configured guild ID in message handling, not only command registration.
- Mark server-only commands as guild-only and explicitly reject interactions without a guild and member.
- Make authorization helpers fail closed for unknown user types.
- Require an administrator ID for telemetry capture, or disable the feature when none is configured.

### 2. Browser extraction permits arbitrary navigation

When enabled, `BrowserClient.extract()` only trims the supplied URL before passing it to Chromium and accepts the resulting redirect destination (`src/nycti/browser/client.py:44`, `src/nycti/browser/client.py:70`, and `src/nycti/browser/client.py:76`). The browser tool is included among generally available read-only tools (`src/nycti/chat/tool_eligibility.py:28`).

This permits server-side request forgery and potentially local-file or internal-service access through URLs such as loopback, private-network, link-local, cloud metadata, non-HTTP schemes, or redirects to those destinations.

Recommended changes:

- Permit only `http` and `https` URLs.
- Resolve and reject loopback, private, link-local, multicast, reserved, and metadata addresses.
- Revalidate every redirect destination and guard against DNS rebinding.
- Add explicit tests for private addresses, alternate IP encodings, non-HTTP schemes, and redirects.

### 3. Container defaults need hardening

There is no `.dockerignore`, meaning local files such as `.env`, `.venv`, caches, and Git metadata can be sent to the Docker build context even though the Dockerfile selectively copies files.

The Dockerfile has no `USER` directive, so the application runs as root (`Dockerfile:1`), despite running AI-triggered Chromium and Python subprocesses. PostgreSQL uses the default `postgres` password and publishes port 5432 on all host interfaces (`docker-compose.yml:9` and `docker-compose.yml:10`). The bot only needs Compose's internal network to reach PostgreSQL.

Recommended changes:

- Add a `.dockerignore` covering `.env`, `.git`, `.venv`, caches, tests, and local artifacts.
- Create and use a non-root runtime user.
- Remove the PostgreSQL host port unless local host access is required; otherwise bind it to `127.0.0.1`.
- Supply the database password through deployment configuration rather than a checked-in default.
- Consider a multi-stage image and pinned base-image digests.

### 4. Database upgrades are fragile

Startup calls `Base.metadata.create_all()` and then performs a small set of hand-written, introspection-based `ALTER TABLE` operations (`src/nycti/db/session.py:28`). This does not reliably handle changes to types, constraints, indexes, defaults, renames, or removals. It also lacks migration version tracking and rollback support.

The current tests do not initialize a real SQLite or PostgreSQL database, so table creation and upgrade paths are not exercised end to end.

Recommended changes:

- Adopt Alembic with versioned migrations.
- Run migrations as a deployment step before starting the bot.
- Add a PostgreSQL CI service test covering a clean database and an upgrade from the previous schema.

### 5. Builds and CI are only partially reproducible

Runtime and development dependencies use broad version ranges without a lock or constraints file (`pyproject.toml:11`). CI installs the newest permitted versions on each run, and Docker base images and GitHub Actions are referenced through mutable tags. A dependency release can therefore break an unchanged commit.

The CI MyPy step checks only three source files (`.github/workflows/ci.yml:22`). That configured subset passes, but `mypy src` reports 225 errors across 29 files, so the repository does not currently have project-wide type-check coverage. The latest Responses API work is among the code not covered by the configured MyPy target.

The new changelog says primary inference switched to GPT-5.6 Luna with high foreground and minimal background reasoning (`src/nycti/changelog.md:5`), but `.env.example` and `Settings` still default to GPT-4.1 Mini/Nano with blank reasoning effort. The Responses adapter only activates for OpenAI model names beginning with `gpt-5.6`, so following the clean setup path does not exercise the claimed migration. If this describes a private production configuration, document that distinction; otherwise align the example and defaults.

Playwright is a mandatory runtime dependency and Chromium is always installed into the Docker image (`Dockerfile:14`), although `BROWSER_TOOL_ENABLED` defaults to `false`. This increases build time, image size, and attack surface for installations that do not use browser extraction.

Recommended changes:

- Generate a reviewed lock or constraints file with hashes using a tool such as `uv` or `pip-tools`.
- Use Dependabot or Renovate to update pinned dependencies intentionally.
- Gradually expand MyPy coverage until `mypy src` is enforced.
- Add an SDK contract/canary test for the clean-config Responses path instead of testing only mocked request construction.
- Add PostgreSQL integration tests and optionally a Python-version matrix.
- Move Playwright into an optional `browser` extra or provide separate minimal and browser-enabled images.
- Pin deployment images and GitHub Actions to immutable digests or commit SHAs where appropriate.

## Smaller Setup Improvements

- The README's local installation command uses `pip install -e .`; contributors running the documented test command also need `pip install -e ".[dev]"`.
- `.env.example` gives optional Tavily configuration a non-empty placeholder, so copying it makes the integration appear configured and produces an authentication failure when used. An empty optional value is less surprising.
- The public repository has no license metadata. Add a license if reuse or external contributions are intended.
- Consider dependency vulnerability scanning and a coverage threshold in CI.

## Positive Observations

- The repository has clear README and agent documentation.
- The source layout and module boundaries are sensible.
- Configuration parsing and validation are centralized.
- `.env` and common generated artifacts are ignored by Git.
- GitHub Actions uses read-only repository permissions.
- The agent loop has explicit budgets, typed tool outcomes, retry/fallback handling, and telemetry.
- Memory behavior is opt-in, bounded, filtered, and covered by retention logic.
- Python execution uses a separate isolated process, AST restrictions, resource limits, and bounded output.
- No tracked secrets were detected during the review.

## Verification Results

The following local checks were run in a Python 3.13 virtual environment:

- `pytest tests/ -q`: **414 passed**
- `ruff check src tests scripts`: **passed**
- Configured CI MyPy targets: **passed**
- `compileall`: **passed**
- `pip check`: **passed**
- Full `mypy src`: **225 errors across 29 files**

Docker was not installed in the review environment, so the Docker image and Compose stack were inspected statically but not built or started.

No tracked source files were modified as part of this inspection.
