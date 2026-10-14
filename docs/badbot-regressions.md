# Bad-Bot Conversation Replays

Seven short turns in `benchmarks/live_cases.json` reproduce two reported failure families with
synthetic prices and generic participants. No private incident bundles are committed.
Existing benchmark artifacts have also been pseudonymized, using reserved synthetic Discord IDs and
generic member labels. This edits the current tree, not historical Git commits. Numeric trace results
remain historical evidence; identity substitutions mean archived artifacts are sanitized copies.

## Cases

- `fixture-badbot-basket-1`: list NVDA/AMD/MU, distinct from the saved GOOG/MSFT defaults; no price lookup.
- `fixture-badbot-basket-2`: "quote those"; retain the explicit three-symbol basket and fetch prices.
- `fixture-badbot-basket-3`: "overnight then"; keep that basket while addressing the session correction.
- `fixture-badbot-basket-4`: "just MU this time"; accept an explicit scope change.
- `fixture-badbot-basket-5`: request an inspirational quote; stop inheriting the stock task.
- `fixture-badbot-fx-1`: "whats USD/JPY?" in a market conversation; provide a grounded current rate.
- `fixture-badbot-fx-2`: "the current rate, not the definition"; retain the pair and fulfill the correction.

The runner feeds actual preceding model answers into later turns, including failed answers. Repeats
have separate histories. Selecting a later scenario turn includes its prerequisites. Scoring never
injects corrections or expected answers into the next user prompt.

Checks cover requested symbols with their frozen prices, successful grounding, missing/extra scope,
and tool-call limits. The scalar checks are not a complete semantic quality judge: review raw answers
for relevance, tone, and whether they addressed the correction. A call-count failure is a signal to
inspect the trace, not a new production limit.

Basket cases accept quote or market_report. The latter uses the production handler, renderer and receipt
builder with frozen Yahoo values. Actual receipts travel with earlier answers into the next synthetic
Discord turn, through the production collector. Offline tests also cover SQL persistence/restart,
cross-user public callbacks, missing symbols, expiry and guild/channel isolation. Private diagnostics
are never supplied as callback context.

Receipts share the existing delivered-response cache and optional persisted diagnostics; only completed
deliveries acquire message links. At most three visible report candidates are exposed, without automatic
selection of a new owner's watchlist. Missing/expired receipts fall back to ordinary visible chat context.

## Run

After approving a separate bounded API budget, run the existing CLI with
`--case-id fixture-badbot-basket-5 --case-id fixture-badbot-fx-2 --mode fixtures --repeats 3`.
Retain complete raw traces and compare both per-turn scores and whole-conversation failures/latency.
Fixture mode still calls paid models. Offline test success does not establish that the deployed model
passes these cases. This change adds tests and guidance; it does not introduce a regex intent router,
cross-user memory access, or a durable conversation-state store.
