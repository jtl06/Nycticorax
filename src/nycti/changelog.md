# Changelog

## 2026-03-20

- folded memory enable/delete actions into a single `/memory` command with `enable` and `forget` options
- compressed `/help` from three pages to two while keeping each page within Discord's message limit

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
