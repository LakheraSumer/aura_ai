# Development progress

Read this first when resuming. Do not assume anything not listed as verified here works; re-run the
tests. Status labels follow spec §36.

## Checkpoint 4 — 2026-11-16: compare-and-book everything, price alerts, lists, web search, Google

**What the user asked for:** compare the same product or service across providers and pick the cheapest,
plus bookings (movies/events, food, travel, tables), web search (prices, news, IPOs), reminders with
follow-ups, order/price-drop tracking, shopping lists, and email + calendar once Google is connected.

**Key point (unchanged):** none of the sellers below gives outside apps price or booking access, so
comparison prices and bookings are TEST data from the offers engine. Every reply says TEST and links the
seller's real page (links checked in a browser on 2026-11-16). Web search and Google are real.

| Capability | Status | Evidence |
|---|---|---|
| Offers engine: identical item priced at every source (products: Amazon/Flipkart/Croma/Reliance Digital/Myntra; food: Swiggy vs Zomato; flights: airline site/MakeMyTrip/Trip.com/Booking.com; hotels: Booking.com/Agoda/MakeMyTrip/Trip.com; buses: redBus/MakeMyTrip; events: BookMyShow/District; tables: Dineout/District by discount; rides: Uber/Ola/Rapido fare estimates). Ranked by the full total (fees, taxes, discounts), with "saves ₹X vs the same item elsewhere" | WORKING_IN_MOCK (unit + integration + live via Gemini) | `test_offers.py` (ranking/totals for all 8 verticals, same-item grouping, savings, per-day determinism). Live: "iPhone 16 sabse saste mein kahan milega?" → 4 stores, Flipkart ₹71,704 cheapest, saves ₹553; "kal Pune se Delhi ki flight, 2 log" → 6 flights × 4 sites |
| Pick → hold (re-quoted) → exact-amount approval → TEST booking code; free table reservations need a confirmation, no payment | VERIFIED_WORKING (integration + live) | `test_compare_product_pick_cheapest_approve_and_cancel`, `test_free_table_reservation_needs_confirmation_but_no_payment`. "1" / "sabse sasta" / "Flipkart wala" are resolved in code into "book offer OFR-…", planned deterministically (`direct-v1`), never by the model |
| Razorpay path for offer bookings (purpose `offer_booking`; booking confirmed only after Razorpay confirms; paid-after-cancel recorded as refund) | IMPLEMENTED (same PaymentWorkflow as groceries; not separately integration-tested) | `activities._finish_offer_payment` |
| Cancel a booking with the refund under that offer's own policy (fee per traveller, free-until date, 12-hour bus rule, non-refundable, already started) | VERIFIED_WORKING (unit + integration) | `test_refunds_follow_each_offers_own_policy` |
| Order/booking status includes offer bookings | VERIFIED_WORKING (integration) | same product test |
| Unrelated reply to a comparison is a new request (not glued to it, not a booking) | VERIFIED_WORKING (integration) | `test_unrelated_reply_to_a_comparison_is_a_new_request` |
| Price-drop alerts: PriceWatchWorkflow checks the same item, messages once on target / 3% drop, ends after N days; list/stop | VERIFIED_WORKING (integration, TEST prices) | `test_price_drop_alert_messages_the_user` |
| Shopping list: add/remove/show (forwardable text)/clear; "order my list" → grocery comparison + cart + approval | VERIFIED_WORKING (integration + live) | `test_shopping_list_add_show_and_order`; live "add milk, atta and eggs to my shopping list" |
| Web search: Google News RSS (news, IPOs, prices) + Wikipedia; Tavily when `TAVILY_API_KEY` is set. Results shown as title/source/date/link, never fed back to the planner. IPO replies add NSE/BSE official pages and say live subscription/GMP data needs exchange licensing | VERIFIED_WORKING (live, free sources); Tavily IMPLEMENTED_UNVERIFIED (no key) | Live "aaj ki IPO news" → 5 dated headlines. `test_google_news_rss_is_parsed_to_titles_links_and_dates` drops a `javascript:` link |
| Reminder follow-ups: ✅ Done / ⏰ Snooze 10 min buttons on the reminder; one nudge after `REMINDER_FOLLOWUP_SECONDS` unless done or snoozed; snooze max 3 | VERIFIED_WORKING (integration) | `test_reminder_follow_up_done_and_snooze` (also: another user's reminder → NotFound) |
| Google connect (OAuth 2.0 + PKCE S256, one-time hashed state, encrypted verifier and tokens), Gmail inbox / send approved drafts, Calendar list / create (approval, no invitees, idempotent event id), disconnect | VERIFIED_WORKING against mocked Google APIs; IMPLEMENTED_UNVERIFIED against real Google (needs a Google Cloud OAuth client + fixed public URL) | `test_google_connect_inbox_and_calendar_with_approval`: state reuse → 400, refresh token not in ciphertext, nothing written before approval |
| Full suite in a container | VERIFIED_WORKING | **149 passed, 3 skipped** (browser tests) |

## Checkpoint 3 — 2026-11-14 (evening): commerce, travel, transport and services layer

Plan and evidence: [integrations/PLAN.md](integrations/PLAN.md),
[integrations/CAPABILITY_MATRIX.md](integrations/CAPABILITY_MATRIX.md),
[integrations/SETUP.md](integrations/SETUP.md).

**Key finding:** none of the requested grocery, food, ride, home-service or travel providers offers
self-serve ordering or booking.
- **Partner approval needed:** Swiggy (Builders Club), Zomato (MCP), Uber, Booking.com, Skyscanner,
  Trip.com.
- **No public ordering API:** Blinkit, Zepto, BigBasket, Flipkart Minutes, Rapido, Urban Company,
  MakeMyTrip, Agoda, redBus.

So transactions run against TEST sandboxes, and real providers get verified handoff links and honest
explanations. Nothing simulated is reported as real.

| Capability | Status | Evidence |
|---|---|---|
| Capability registry: 21 requested providers + 2 sandboxes, per-operation status, `require()` enforcement, credentials → live only when the adapter exists | VERIFIED_WORKING (unit) | `test_registry_security_payments.py`: no real provider claims live ordering/booking; partner/handoff explanations; PhonePe stays credentials-required with keys because no adapter exists |
| Normalised provider types (money in paise, price breakdown, quote lines with pack size and base quantity, quote expiry, provider reference) | IMPLEMENTED (used by grocery quotes) | `providers/types.py`, `grocery_quote.py` |
| Live weather (Open-Meteo geocoding + forecast; source, timestamp, units, timezone) | VERIFIED_WORKING (live) | Real calls for Pune and Ambajogai; "kal Pune mein baarish hogi?" answered with tomorrow highlighted. Non-commercial terms |
| Grocery: equivalent quantities ("2 litre" = cheapest in-stock pack mix per store), store subset, cheapest/fastest, fees in totals, serviceability, minimum order | WORKING_IN_MOCK (unit + integration + live replay) | Acceptance example live: asked which milk → Blinkit vs Zepto both ₹215 incl. fees → cart. Zepto picks 4×500 ml where cheaper |
| Variant question, no silent substitution (substitution/skip/other-store question) | VERIFIED_WORKING (unit + integration) | `test_acceptance_blinkit_or_zepto_cheapest_total`, `test_answers_to_grocery_questions_resolved_in_code` |
| Cart edits ("ek aur 6 eggs add kar do", "remove bread"), cancel, order status | WORKING_IN_MOCK (integration + live) | Live: 12 + 6 eggs → 12-pack + 6-pack; status "ON THE WAY (simulated)" |
| Checkout summary (provider, items, address, breakdown, compared totals, quote validity, cancellation terms, payment method, real-provider limit + link) | VERIFIED_WORKING (live replay) | `scripts/commerce_replay.py` |
| Exact-transaction approval for payment (amount, items, address, method bound by action hash), single use | VERIFIED_WORKING (integration) | Replayed approval → 409; changed cart → FAILED, nothing charged |
| Quote expiry + changed price → renewed approval required; old approval can't charge | VERIFIED_WORKING (integration) | `test_changed_price_after_quote_expiry_needs_new_approval` |
| Old Pay button after a cart edit charges nothing | VERIFIED_WORKING (integration) | `test_old_pay_button_after_cart_edit_charges_nothing` |
| Razorpay payment links: create (idempotent per step), signed webhook (HMAC over raw body), event-id dedupe, status API reconciliation | IMPLEMENTED_UNVERIFIED against real Razorpay (no keys); VERIFIED_WORKING against a mocked Razorpay API | `test_payment_link_webhook_places_order_once`: forged webhook 403, duplicate ignored, order placed once, one confirmation, one link |
| PaymentWorkflow (Temporal): payment pending → paid / failed / expired, webhook fast path + polling, completion exactly once | VERIFIED_WORKING (integration, mocked gateway) | `test_unpaid_link_expires_and_nothing_is_ordered`: no webhook → reconciliation finds "expired" → cart cancelled, user told nothing was ordered |
| Crash with an ambiguous payment outcome is reconciled, not repeated | VERIFIED_WORKING (integration) | `test_interrupted_payment_is_reconciled_not_repeated` (payment step is deliberately not in IDEMPOTENT_SIDE_EFFECTS) |
| Provider account linking: AES-GCM tokens bound to user+provider, revocation | VERIFIED_WORKING (unit + integration) | Ciphertext copied to another user fails to decrypt; revoked token unreadable |
| Secure one-time OTP page (provider + purpose shown, 5 min TTL, 3 attempts, single use, wiped after consumption, payment purposes refused) | VERIFIED_WORKING (integration); no live provider uses it yet | `test_otp_secure_page_flow` |
| Chat redaction of card/CVV/PIN/OTP/password before storage and planning | VERIFIED_WORKING (unit + integration + live) | Live: "my upi pin is 4321" → masked + warning, no task |
| Rides / food / home / travel / bus handoffs with verified links and per-app limits | VERIFIED_WORKING (live replay) | Hotel (Booking.com search link with dates), cab (Uber universal link), Zomato |
| Integration suite runs in a container (Smart App Control blocks the host) | VERIFIED_WORKING | **120 passed, 3 skipped** (browser tests skipped: no Chromium in the image) |
| Gemini Google Search grounding | BLOCKED_EXTERNAL_ACCESS | Free key: 429 on every grounded request |
| Real grocery/food/ride/hotel transactions | BLOCKED_EXTERNAL_ACCESS (partner access) | See SETUP.md |

## Checkpoint 2 — 2026-11-14: live WhatsApp test number, free-tier AI planner, containers

### What changed and its status

| Capability | Status | Evidence |
|---|---|---|
| WhatsApp Cloud API **inbound** on Meta's test number (+1 555-160-5093) via webhook | VERIFIED_WORKING | Meta's handshake reached `/v1/webhooks/whatsapp` (200). Real phone messages arrived signed, were stored, and created tasks (2026-11-14 05:56–06:01 UTC) |
| WhatsApp **outbound** reply to the owner's phone | BLOCKED_EXTERNAL_ACCESS | Meta has restricted the WhatsApp Business Account: the dashboard shows "Account restricted, Duration: Permanent. You can't start conversations / respond to messages / add phone numbers" until business verification. API health: WABA `can_send_message: BLOCKED` (141006 payment method), business LIMITED (141010 not verified). Sends fail with `131031 Business account locked`; Meta's own Hello World test did not arrive either. Earlier: `131030` until the number was added to the recipient list |
| Our WhatsApp test-number allow-list gate | VERIFIED_WORKING | A message from a number not in `WHATSAPP_ALLOWED_TEST_NUMBERS` (919284…) got `BLOCKED_POLICY` and nothing was sent |
| **Telegram bot (@Instinct_AI_ML_bot) live round trip** | VERIFIED_WORKING | `setWebhook` with `secret_token` to the webhook-only instance. Real "hi" from a private chat → Gemini plan → reply `SENT` 4 s later (2026-11-14 07:04 UTC). Unsigned POST → 403 |
| Telegram button taps acknowledged (`answerCallbackQuery` as the webhook reply body) | VERIFIED_WORKING (unit) | Not yet tapped live |
| Unsupported requests (movie/flight tickets, payments, other websites) → one honest "can't do that yet" reply in 4 languages | VERIFIED_WORKING (live planner + unit) | Live Telegram bug: "book ticket ramayana 2 person tonight pune" became a train search, and the follow-up became the train's destination. Now: prompt rule + grounding (`unsupported_request`); Gemini live check plans both as unsupported |
| Browser steps only for allow-listed site keys, and only when a browser worker runs (`BROWSER_ENABLED`) | VERIFIED_WORKING (unit) | Live bug: Gemini invented `bookmyshow` site keys; with no browser worker in the Docker stack the tasks hung RUNNING (terminated manually) |
| Train clarification accepts only a city/station as the answer | VERIFIED_WORKING (unit) | Otherwise the reply is treated as a new request |
| `answer_question` tool: general questions (recipes, facts) answered directly | VERIFIED_WORKING (live planner + unit) | Biryani recipe and "capital of Australia" answered by Gemini |
| Mixed requests: do the supported parts, name only the unsupported part | VERIFIED_WORKING (live planner + unit) | "recipe + order from Blinkit" → recipe + "can't: ordering from Blinkit"; "train + book flight" → IRCTC link + "can't: booking flights" |
| Movie tickets: BookMyShow **showtimes page for movie + city + date** (`find_movie_tickets`) | VERIFIED_WORKING (link only; live replay) | Format from the owner's own link, checked in a real browser: `/movies/{city}/{slug}/buytickets/{ET}/{YYYYMMDD}` (Ambajogai and Pune). The ET code comes from `movie_catalog` (migration 0002): 16 movies read from BookMyShow's Pune listing on 2026-11-14, plus codes learned from BookMyShow links users send. Unknown movie → city page + "send me its link once". City missing → asks. No booking automation: BookMyShow blocks bots (Cloudflare challenge) and booking needs the user's login and payment. Never shows showtimes, prices or seats |
| **In-chat movie booking, sandbox cinema** (`book_movie_tickets` → `pay_movie_booking`): theatre → show → requested rows → seats together → hold 15 min → L3 payment approval bound to exact seats + amount → TEST booking ID, read back | WORKING_IN_MOCK (live replay, 4 scenarios) | `scripts/cinema_replay.py`: (1) row A at City Pride Kothrud 10:45 PM → held → approved → `TEST-…` confirmed; (2) housefull 7 PM → 6 alternatives incl. other theatres → chosen → payment rejected → cancelled; (3) row D can't seat 8 → row K offered → held; (4) no theatre → theatre list → "kothrud" → show list → "10:45 wala" → held. Replies to option lists are resolved in code (`resolve_cinema_choice`) after the free model misread a long options prompt live. Availability, payment and booking IDs are simulated; every reply says TEST and links the real BookMyShow page. Migration 0003 |
| **In-chat grocery ordering, sandbox quick commerce** (`order_groceries` → `pay_grocery_order`) | WORKING_IN_MOCK (live replay, 6 scenarios) | `scripts/grocery_replay.py`: (1) named store → cart → approved → `TEST-ORD-…`; (2) recipe + ingredients → recipe and cart → rejected; (3) Hinglish, no store → 5 apps compared (missing items flagged) → "sabse sasta" → approved; (4) item out of stock → rest of cart + where it's available; (5) no address → asked → cart; (6) "remove bread, add 6 eggs" → new cart, old Pay button refused ("replaced, nothing charged"), new one placed. Known gap: the free model sometimes lumps recipe spices into one item ("spices packet"), which is flagged as not found. Migration 0004 |
| Gemini Google Search grounding (to find unknown movie IDs) | BLOCKED_EXTERNAL_ACCESS | Free key returns 429 on every `google_search` request while plain calls work. Needs a paid tier or another search API |
| Relative reminder times ("in 2 minutes", "10 min baad", "2 ghante mein", "5 मिनट बाद", "१० मिनिटांनी", "half an hour") | VERIFIED_WORKING (unit + live planner) | Live bug: "remind me in 2 minute…" asked "When?" |
| AM/PM resolved when only one reading is still ahead ("12:45" at 12:43); options keep minutes | VERIFIED_WORKING (unit) | Live bug: "Is that 12:00 or 00:00?" asked twice for 12:45 |
| Reminder subject kept across clarification rounds | VERIFIED_WORKING (unit) | Live bug: the reminder fired as just "Reminder" ("pay electricity bill" lost) |
| Telegram live: reminder fired on time; email draft → Approve button → sandbox send | VERIFIED_WORKING | Owner's Telegram chat, 2026-11-14 12:43–12:45 IST |
| "Can't do" / "what can I help with?" notes finish the task (no waiting); superseded tasks don't re-send | VERIFIED_WORKING (live replay + unit) | Live bug: the movie reply's "can't pay" note kept the task waiting, the next message ("pulav recipe") superseded it, and the old task re-sent the movie link. Replay now: movie COMPLETED, pulav gets only the recipe (fixes OPEN_ISSUES #3 for this path) |
| Webhook registration + app→WABA subscription via Graph API | VERIFIED_WORKING | `POST /{app}/subscriptions` and `POST /{waba}/subscribed_apps` both `success: true`, done with the owner's approval |
| Webhook-only public instance (ADR-012) | VERIFIED_WORKING | Through the tunnel: register/docs 404, bad verify token 403, unsigned POST 403; unit test |
| Cloudflare quick tunnel (`cloudflared 2026.11.0`) | WORKING_IN_SANDBOX | Temporary URL that changes on every restart. Not for production |
| Gemini planner (`gemini-3.1-flash-lite`, free tier, OpenAI-compatible endpoint) | VERIFIED_WORKING (live, small sample) | Smoke test on the live stack: `planner=gemini-planner-v1`, reminder SCHEDULED Sun 15 Nov 08:00 IST, reply draft created. Six-request manual check (ADR-011). No accuracy eval yet |
| OpenAI planner (`gpt-5.4-mini`) | IMPLEMENTED_UNVERIFIED | Request shape unit-tested. Live calls returned 429 "no credits remaining" |
| Claude planner | IMPLEMENTED_UNVERIFIED | Unchanged; no key |
| Planner provider error → offline rule planner fallback | VERIFIED_WORKING | Live: OpenAI 429 → `planner_fallback` event → reminder still scheduled and replied. Unit test |
| API commits before responding (`Depends(get_db, scope="function")`) | VERIFIED_WORKING | Bug found in the container smoke run: a request right after register got 401 "Account not active" |
| Reply after the "what can I help with?" prompt is a new request (not merged) | VERIFIED_WORKING (unit) | Bug seen live: reminder text became "hi mummy ke liye medicine" |
| App runs as containers (`scripts/docker_stack.ps1`) | VERIFIED_WORKING (local) | api + worker + webhook instance on the compose network; smoke test passes |

### Test results (latest)

| Run | Result |
|---|---|
| `pytest tests/unit` inside `apna-backend:dev` (source mounted) | **82 passed** |
| `ruff check services/api/src tests scripts` | All checks passed (ruff previously never covered the root `tests/` or `scripts/`; now it does) |
| Integration suite | **Not run this checkpoint.** Windows Smart App Control now blocks native modules on the host (Temporal's bridge, jiter, SQLAlchemy's Cython), and the suite isn't containerized yet. See OPEN_ISSUES #7 |
| Live smoke test (containers, Gemini) | reminder COMPLETED/SCHEDULED, email draft COMPLETED |

### Secrets shared in chat (rotate after testing)

The OpenAI key, Meta App secret and WhatsApp access token were pasted into the conversation. They live
only in `services/api/.env` (git- and docker-ignored). Rotate all three after testing. The Gemini key
was saved through the hidden prompt (`scripts/set_secret.ps1`) and later also pasted in chat.

## Checkpoint 1 — 2026-11-12: foundation + first vertical slice

**Channel decision:** chat-only: WhatsApp (official Cloud API, test mode) plus Telegram. No app and no
website (ADR-010).

### Feature status

| Capability | Status | Evidence |
|---|---|---|
| Local infra: Postgres 16 + pgvector, Redis 7.4, Temporal 1.5.1 dev server (docker compose) | VERIFIED_WORKING | `docker compose up -d`; all healthy |
| DB schema (19 tables) + Alembic migration `0001` | VERIFIED_WORKING | upgrade → downgrade → upgrade on Postgres; integration suite builds on it |
| Auth: register / login / refresh rotation (Argon2id, JWT, hashed refresh tokens) | VERIFIED_WORKING | integration tests register users; `test_ownership_isolation` |
| Row ownership (no cross-user task access) | VERIFIED_WORKING | `test_ownership_isolation` |
| Language detection: en / hi / mr / Hinglish | WORKING_IN_MOCK (heuristic) | `tests/unit/test_language.py` (6 utterances) — tiny eval set, not accuracy-measured |
| Date/time resolution in IST: kal/parso tense check, next-weekday ambiguity, subah/shaam/raat, Devanagari digits, saadhe/sava/paune | VERIFIED_WORKING (unit) | 27 unit cases |
| Claude planner (`claude-opus-5-5`, structured JSON output, refusal fallback `"default"`) | IMPLEMENTED_UNVERIFIED | request shape + parsing tested with a fake client; **no live API call made** (no key in env, spending needs approval) |
| Rule planner (offline, deterministic) | WORKING_IN_MOCK | used by every integration test |
| Plan grounding: tool allow-list, Pydantic arg validation, DAG/cycle check, risk derived from registry | VERIFIED_WORKING | `test_planning.py` |
| Policy engine L0–L4 + exact-action approval hash, single-use, expiry | VERIFIED_WORKING | `test_policy_and_hash_binding`, `test_02_03`, `test_03b` (tampered draft not sent) |
| Durable task workflow (Temporal): parallel independent steps, approval waits, clarification waits, cancel, expiry | VERIFIED_WORKING | integration suite |
| Worker crash/restart while waiting on approval → resumes, sends exactly once | VERIFIED_WORKING | `test_14` (see OPEN_ISSUES #1) |
| Reminders: durable Temporal timer, verified RUNNING, idempotent delivery | VERIFIED_WORKING | `test_01`, `test_01b` |
| Email: sandbox mailbox provider (find thread, draft, read-back verification, approved send) | WORKING_IN_MOCK | `test_02_03`, `test_03b`, `test_03c` |
| Gmail / Google Calendar | NOT_IMPLEMENTED | — |
| Train search: station resolution + official IRCTC link; never fabricates trains, fares or seats | WORKING_IN_MOCK (no data source) | `test_04` |
| Isolated browser (Playwright, per-user persistent profile, domain allow-list, screenshots, redacted action log) | VERIFIED_WORKING (local sandbox site) | `test_06` |
| Browser form submit with approval + independent verification against the site record | VERIFIED_WORKING (sandbox) | `test_06` |
| Prompt-injection page: flagged, ignored, no derived actions | VERIFIED_WORKING (one payload) | `test_15` |
| Password/OTP page → user handoff, never typed | VERIFIED_WORKING | `test_16` |
| WhatsApp Cloud API adapter: signed webhooks, verify handshake, text/buttons/status parsing, interactive approval buttons, 24 h window, production gate, dedupe | WORKING_IN_MOCK | unit + `test_whatsapp_*` (recording transport; **nothing sent to Meta**) |
| WhatsApp production | BLOCKED_EXTERNAL_ACCESS | Meta's general-purpose AI restriction (see compliance doc) |
| Telegram Bot API adapter: secret-token webhooks, private chats, inline-keyboard approvals | WORKING_IN_MOCK | unit + `test_12` (no bot token yet) |
| Cross-channel linking (`/linkcode` → `link CODE`), one-time codes, shared conversation | VERIFIED_WORKING (mock transports) | `test_12` |
| Live run as real processes (uvicorn API + worker) | VERIFIED_WORKING (local) | `scripts/smoke.py`: Hinglish reminder SCHEDULED; email draft waiting on approval |
| Backend Docker image (api/worker) | VERIFIED_WORKING (build + import only) | image built; not yet run as a container stack |
| CI workflow (`.github/workflows/ci.yml`) | IMPLEMENTED_UNVERIFIED | no GitHub remote yet |
| Memory, voice, calling, SMS, payments, bookings, proactive monitors, artifacts, group coordination, admin, Azure IaC | NOT_IMPLEMENTED | see NEXT_STEPS |
| Flutter mobile app, Next.js web | NOT_IMPLEMENTED (descoped by ADR-010) | — |

### Test results (latest)

| Run | Result |
|---|---|
| `pytest tests` (unit + integration), after the final code change | **64 passed**, in 5 of 6 runs. One run had 1 failure (`test_04`), not reproduced in 7 later runs: see OPEN_ISSUES #1 |
| Stability: 4 consecutive full runs before the final reminder-text fix | 63/63 each |
| `test_14` + `test_13` + `test_02` looped ×6 | 6/6 passed |
| `ruff check src tests` | All checks passed |
| Docker image `apna-backend:dev` (base target) | built (686 MB); `import apna.api.main, apna.workflows.worker` succeeds inside the container. Browser target not built yet |

Failures seen and fixed during this checkpoint:
- `.test` emails were rejected by email-validator. Fixed by allowing RFC 2606 `.test` outside production only.
- `test_03b` expected the task to finish. Correct behaviour is to wait for the user. Test fixed, and a localized "draft changed" message added.
- Race conditions in `test_13` and `test_03b` (step, task and reply projections are written by consecutive activities). The tests now poll for all of them.
- The duplicate browser step in the rule planner was found by reading the code and fixed. The bad reminder text ("Kal subah 8 baje … laga de") was found in the live smoke run and fixed, with a unit test.

### Files (all new)

`services/api/src/apna/` — `core/` (config, logging, errors), `domain/enums.py`, `db/` (models,
session), `security/auth.py`, `agent/` (language, planner, grounding, tools, policy, executors,
replies, schemas), `providers/` (email sandbox, travel), `browser/` (executor, testsite),
`workflows/` (definitions, activities, worker), `channels/` (base, whatsapp, telegram, outbound),
`services/conversation.py`, `api/` (main, routes, webhooks, deps). Plus `migrations/`, `tests/unit`,
`tests/integration`, `infrastructure/docker/`, `.github/workflows/ci.yml`, `scripts/smoke.py`.

### Security concerns to carry forward

- Browser profiles are plain directories locally. Production needs encrypted volumes and a per-tenant
  network egress policy.
- The JWT secret has a dev default. Production must load it from Key Vault, and startup should refuse
  the default.
- Webhook endpoints have no rate limiting yet (Redis is provisioned but not used).
- Channel-native accounts orphaned by linking are not cleaned up.
