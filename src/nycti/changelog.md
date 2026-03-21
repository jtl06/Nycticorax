# Changelog

## 2026-03-20

- folded memory enable/delete actions into a single `/memory` command with `enable` and `forget` options
- compressed `/help` from three pages to two while keeping each page within Discord's message limit
- tightened `prompt.md` slightly for shorter default replies and less redundant wording
- added Tavily Extract as a dedicated URL-content tool so the model can summarize specific pages without using search
- added multimodal image-attachment input for triggered messages when the configured chat model supports image input
- added optional `OPENAI_VISION_MODEL` routing so image-bearing requests can use a separate vision-capable model
- fixed image-bearing chat requests for OpenAI-compatible providers that require `max_completion_tokens` instead of `max_tokens`
- added fallback retries for providers that still reject image requests over token-field conflicts
- added optional `OPENAI_EMBEDDING_MODEL` support for hybrid semantic + lexical memory retrieval
- added bounded reply-chain and linked-message context so replies can include referenced text and images from same-guild Discord messages
- fixed `/benchmark earnings` after image-routing changes by passing the required empty image context for non-image benchmark runs
- made memory extraction more liberal by broadening durable-memory signals and adding a small confidence grace window for strong personal facts, goals, and routines
- added a separate per-user `/show memory` overlay so replies can show retrieved memories and memory state without enabling latency debug
- clarified the system prompt so Nycti explains memory correctly instead of claiming long-term memory is external or unavailable
- added Tavily-backed image search so Nycti can fetch direct image URLs for “what does this look like” requests and let Discord embed them
- added bounded startup retry/backoff for Discord 429 / Cloudflare 1015 login failures to avoid tight crash loops on temporary edge rate limits
- extended multimodal context so recent channel-context image attachments can also be passed to the vision model with source labels
- changed image handling so `OPENAI_VISION_MODEL` runs a separate image-summary prepass while the main chat/tool loop stays on `OPENAI_CHAT_MODEL`
- fixed Clarifai-backed embeddings by sending a minimal direct request to Clarifai's OpenAI-compatible `/embeddings` endpoint and retrying Clarifai model-name variants when the full model URL is rejected
- increased the chat tool-loop cap to 6 total rounds to allow slightly more search/extract iteration before forcing a final answer

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
