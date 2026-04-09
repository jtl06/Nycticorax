# Changelog

## 2026-04-08

- replaced the Alpaca market-data integration with Twelve Data so `stock_quote` can cover broader supported symbols like stocks, ETFs, indexes, and some futures, and now returns nearby symbol suggestions when a direct quote symbol is unsupported
- extracted Discord message-context collection helpers into `src/nycti/message_context.py` so `bot.py` keeps the higher-level runtime flow while reply-chain, linked-message, and image-context assembly live in a dedicated module
- extracted the vision prepass and data-URI image-preparation flow into `src/nycti/vision.py` so `bot.py` no longer owns low-level multimodal preprocessing and image-download logic

## 2026-04-04

- added an Alpaca-backed `stock_quote` chat tool so Nycti can fetch current U.S. stock snapshots directly instead of relying on web search for live price numbers
- tightened memory extraction to reject temporary shopping intent, promo/deal hunting, and one-off link-request state so long-term memory stays focused on durable preferences, projects, plans, and lore
- added optional `DISCORD_ADMIN_USER_ID` support so `/memories userid:<id>` can inspect another user's stored memories when explicitly allowed, while defaulting to your own memories when `userid` is omitted
- switched memory embeddings back to standard OpenAI embeddings and added optional `OPENAI_EMBEDDING_API_KEY` so embedding traffic can use a separate OpenAI key even when chat completions still point at another provider via `OPENAI_BASE_URL`
- removed the Clarifai-specific embedding request path and tightened blank embedding-input handling so memory retrieval/store calls do not send empty embedding payloads
- made vision-prepass failures log the configured vision model, image count, and exception text so image-analysis outages and provider mismatches are easier to diagnose from logs
- changed image routing so if `OPENAI_VISION_MODEL` fails, Nycti falls back to direct multimodal input on `OPENAI_CHAT_MODEL` instead of dropping image context entirely
- extended the memory debug block to show whether embeddings use a separate API key and whether they should target OpenAI directly or reuse the shared base URL, without exposing secrets
- added a Clarifai Gemini image-input workaround that downloads external image URLs and sends them as base64 data URIs for affected multimodal requests
- added optional `OPENAI_EMBEDDING_BASE_URL` support so embeddings can use a different OpenAI-compatible endpoint than chat completions

## 2026-03-20

- added `OPENAI_CHAT_MODEL_FALLBACKS` so reply generation can fail over to backup chat models when a provider model URL goes bad instead of taking the bot offline
- added support for direct Clarifai embedding `/outputs` endpoints so memory embeddings can use non-serverless Clarifai models like Qwen embedding deployments
- trimmed the README slightly to reduce repetition in the feature, image, and env sections
- replaced the full README project tree with a shorter key-modules overview
- rewrote the README intro blurb to describe Nycti's role and capabilities instead of implementation details
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
