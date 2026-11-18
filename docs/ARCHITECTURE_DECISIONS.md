# Architecture Decision Records

Each ADR is short and dated. Status: Accepted unless stated. Later ADRs may supersede earlier ones.

---

## ADR-001 — One Python backend package, several deployable processes (2026-11-12)

**Context.** The spec lists `services/api`, `agent-runtime`, `workflow-worker`, `browser-worker`, etc.
Splitting these into separate Python projects on day one would duplicate models, schemas and
settings, and make cross-service refactors expensive while the domain is still moving.

**Decision.** A single installable package `apna` lives in `services/api/src/apna`, with
sub-packages that map 1:1 to future services:

| Sub-package | Future deployable | Entry point |
|---|---|---|
| `apna.api` | `services/api` (FastAPI) | `uvicorn apna.api.main:app` |
| `apna.agent` | `services/agent-runtime` (in-process for now) | imported by workflows |
| `apna.workflows` | `services/workflow-worker` | `python -m apna.workflows.worker` |
| `apna.browser` | `services/browser-worker` | `python -m apna.browser.worker` (Temporal task queue `browser`) |

Each deployable gets its own Dockerfile and its own process; only the code is shared.
**Consequence.** Splitting a sub-package out later is a packaging change, not a rewrite.
Import boundaries are enforced by convention: `apna.api` must not import `apna.browser`
internals; it talks to the browser worker only through Temporal.

## ADR-002 — Temporal owns execution state; PostgreSQL holds the queryable projection (2026-11-12)

**Context.** The spec suggests LangGraph *and* Temporal and warns against two competing
state machines.

**Decision.**
- **Temporal** is the only engine that decides "what runs next" for a task: step ordering,
  retries, timers (reminders), waiting for approval signals, resumption after a crash.
- **PostgreSQL** stores the *projection* that users and the UI read: `tasks.status`,
  `task_steps.status`, `approvals`, `tool_executions`, `workflow_events`. Projections are
  written only by Temporal activities, never by the API directly (except creating the task
  row and recording an approval decision before signalling).
- **LangGraph is not used.** The model's role is bounded: it produces a typed plan (one
  structured-output call) and, later, summarises. A graph framework adds a second checkpoint
  store for no benefit at this scope. Revisit if we add open-ended multi-turn tool loops.

**Consequence.** After a worker restart, Temporal replays the workflow; activities are
idempotent (keyed by `step_id`), so an externally-executed action is never repeated
(see ADR-005).

## ADR-003 — Claude is the planner; deterministic code is the authority (2026-11-12)

**Decision.**
- Planning: `claude-opus-5-5` via the official `anthropic` Python SDK, structured output
  (`output_config.format` JSON schema) producing a `TaskPlan`. Server-side refusal
  fallback is enabled (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`).
- Every tool argument the model proposes is re-validated by a Pydantic schema in the tool
  registry. Unknown tools are rejected.
- Risk level, approval requirements and allowed recipients are computed by
  `apna.agent.policy` from the tool's static declaration, never from model output.
- User-facing completion text is rendered from **verified tool results** in the database,
  not from model prose — the model cannot claim a booking or a send that did not happen.
- When no Anthropic credential is configured (`DEVELOPMENT_MODE=true`), a deterministic
  rule-based planner (`apna.agent.planner.RulePlanner`) is used. It is labelled
  `WORKING_IN_MOCK` and exists so the platform is testable without paid credentials.

## ADR-004 — Approvals are bound to an exact action hash (2026-11-12)

An approval row stores `action_hash = sha256(canonical_json(tool, args))`. Executing a
Level ≥2 tool requires an approved, unexpired approval whose hash matches the arguments
being executed. Any change to recipient, body, amount or merchant changes the hash and
requires re-approval. Enforced in `apna.agent.policy.assert_authorized`, called inside the
activity right before the side-effect.

## ADR-005 — Idempotent side-effects via `tool_executions` (2026-11-12)

Each step execution writes a `tool_executions` row with a unique `idempotency_key`
(`task_id:step_id`). Before a side-effect the activity checks for an existing
`SUCCEEDED` execution and returns its stored result. Provider calls also receive the
idempotency key where the provider supports it.

## ADR-006 — Provider contracts with honest mocks (2026-11-12)

Every external capability (email, calendar, trains, telephony, payments…) is a Python
`Protocol` in `apna.providers.contracts` with:
1. A **mock/sandbox** implementation that behaves deterministically and is labelled as such
   in every result (`provider="mock-email"`, `environment="mock"`).
2. A **real** implementation only when we have the credentials to verify it.
3. An **official-link fallback** for travel domains (e.g. IRCTC) that never fabricates
   availability, fares or PNRs.

## ADR-007 — Browser isolation model (2026-11-12)

- Playwright Chromium, one **persistent context per user** stored under
  `BROWSER_PROFILE_ROOT/<user_id>/` (encrypted-at-rest is a deployment concern: Azure Files /
  Blob with CMK; locally it's a plain directory — documented in KNOWN_LIMITATIONS).
- A **domain allowlist** is checked on every navigation and every request (route
  interception); off-list requests are aborted.
- Page text returned to the model is wrapped as *untrusted data*; the browser executor never
  takes actions from page text, only from validated plan steps.
- Password/OTP fields are detected and trigger `WAITING_FOR_USER` handoff; the executor
  refuses to type into them.

## ADR-008 — Dates and language (2026-11-12)

Default timezone `Asia/Kolkata`. "kal" is resolved to *tomorrow* only when the sentence is
future-oriented (imperative/request verbs: "laga de", "remind", "dekh", "kar"); with
past-tense markers ("tha", "gaya", "hua") it is ambiguous and the agent asks. "parso" is
treated the same way (±2 days). Times without AM/PM are disambiguated by period words
(subah/सकाळी = morning, shaam/संध्याकाळी = evening, raat = night); if still ambiguous the
agent asks rather than guessing.

## ADR-010 — Chat-only product: WhatsApp (gated) + Telegram; no app, no website (2026-11-12)

**Context.** The product owner chose chat channels only: no mobile app and no website. The owner's
preferred channel is WhatsApp. Meta's WhatsApp Business Solution terms (announced Oct 2025, in force
2026-01-15) bar general-purpose AI assistants from the WhatsApp Business API; business-specific
customer-service bots remain allowed. Unofficial WhatsApp automation breaks WhatsApp's terms and is
excluded.

**Decision.**
- WhatsApp is built against the **official Cloud API only**, verified with signed webhooks, and
  tested with Meta's test number. Production delivery is gated by `WHATSAPP_PRODUCTION_ENABLED`
  (default false). With it off, only numbers in `WHATSAPP_ALLOWED_TEST_NUMBERS` receive messages;
  everything else is recorded as `BLOCKED_POLICY`.
- **Telegram Bot API** is the launchable channel, because bots and AI assistants are permitted there.
- Both channels share one conversation/task backend. A user links chats with `/linkcode` in one chat
  and `link CODE` in the other (one-time, 10 minutes). Accounts are never merged by phone number.
- Approvals are native reply buttons (WhatsApp interactive buttons, Telegram inline keyboard). An exact
  short "yes"/"haan" also counts, but only when exactly one approval is pending in the conversation.
- WhatsApp's 24-hour customer-service window is enforced: outside it only an approved template may be
  sent; without one the message is not sent.
- The REST API remains for testing, admin tooling and future channels. It is not a consumer surface.

**Supersedes** the Flutter + Next.js parts of the original spec (§3, §25). The spec's account and
privacy screens become chat commands (to be built: "what do you know about me", "delete my data").

## ADR-009 — Local ports (2026-11-12)

Postgres `55432`, Redis `56379`, Temporal gRPC `57233`, Temporal UI `58233`, API `8800`,
web `3800`. Chosen to avoid clashing with other local stacks on default ports.

## ADR-011 — Pluggable planner providers with an offline fallback (2026-11-14)

**Decision:** the planner is selected by `PLANNER_PROVIDER` (`auto | anthropic | openai | gemini | rule`).
Claude stays the primary design target. OpenAI and Google Gemini share one implementation
(`OpenAIPlanner`) through the OpenAI Chat Completions API with strict JSON-schema output; Gemini uses
Google's OpenAI-compatible endpoint. All providers use the same frozen system prompt, plan schema,
grounding, tool allow-list and policy engine, so no provider can change risk levels or invent dates.

If a provider call fails (network, auth, rate limit, no credit, outage), the SDK error becomes
`PlannerUnavailable` and `plan_task` falls back to the deterministic `RulePlanner` for that task and
records a `planner_fallback` event. An explicitly chosen provider without a key resolves to the rule
planner; it never silently switches to a different paid provider.

**Why:** the owner tests without paid credit (OpenAI returned 429 "no credits"; Gemini's free tier is
used for test data only). Before this change a provider error was retried by Temporal until the
workflow failed, and the user got no reply at all.

**Measured (2026-11-14, free tier, IST):** `gemini-3.1-flash-lite` without extra reasoning planned and
grounded Hinglish, Marathi (Devanagari) and English requests in ~1.5 s typically (some calls ~30 s).
`gemini-3.5-flash` gave slightly better email plans but took 15–66 s. `gemini-2.5-flash` is closed to new
users (404); `gemini-3.8-flash` timed out. Six-request check only; not an accuracy eval.

## ADR-012 — Public exposure only through a webhook-only instance (2026-11-14)

**Decision:** anything reachable from the internet (tunnel now, ingress later) points at a separate
API instance started with `WEBHOOK_ONLY=true`, which mounts only `/v1/webhooks/*` and `/healthz` (no
auth, chat, task routes, OpenAPI or docs). The full API binds to `127.0.0.1:8800` only.

**Why:** a public URL on the full API would let anyone register and spend planner credit. Verified
through the tunnel: register/docs → 404, wrong verify token → 403, unsigned POST → 403.

## ADR-013 — In-chat bookings run against a sandbox provider until a partner API exists (2026-11-14)

**Decision:** movie booking in chat (theatre, show, rows, seat check, alternatives, payment, booking)
runs against `providers/cinema.py`, a sandbox with the shape of a ticketing partner API. Payment is a
Level-3 step whose approval binds the exact seats and amount (`prepare_movie_payment`); approving it is
the "pay" action. Holds expire after 15 minutes; a lapsed hold is booked only if the same seats are still
free. Every reply is labelled TEST and links the real BookMyShow showtimes page.

**Why:** BookMyShow (and the quick-commerce apps) offer no public ordering API: only partner deals.
Their sites block bots, and orders need the user's own login and payment, so automating them is not an
option. The owner chose the sandbox to get the full conversation built and tested now; swapping in a real
partner provider later changes only the provider module.

**Rejected:** browser automation of BookMyShow (bot protection, user credentials, payments); letting the
model resolve replies to option lists (it misread "kothrud" in a live run, so they're matched in code).

**Same pattern for quick commerce (2026-11-14):** `providers/grocery.py` simulates Blinkit, Swiggy
Instamart, Zepto, Flipkart Minutes and BB Now (catalogue of ~55 common items with Hindi/Marathi names,
per-store prices, stock-outs with substitutes, delivery/handling fees, ETAs). `order_groceries` builds or
compares carts; `pay_grocery_order` is L3, bound to the exact items and total. A new cart replaces the
user's older unpaid carts, so an old Pay button can't place a stale order. Real ordering would need ONDC
(registered buyer app) or partner agreements, plus a payment-link provider (Razorpay/Cashfree) for
paying inside chat.

## ADR-014 — Capability registry with honest per-operation status (2026-11-14)

**Decision:** every provider is registered in `providers/registry.py` with a status per operation:
`live`, `sandbox`, `credentials-required`, `partner-access-required`, `handoff-only` or `unsupported`.
Each entry carries evidence links and the verification date. `require()` raises
`CapabilityUnavailable` with a plain explanation and the provider's verified handoff link. Credentials
alone never make an operation live: the adapter must exist too. Partner-only operations stay disabled
until an adapter is written against the provider's real docs.

**Why:** the request named 20+ providers, most with no public ordering API. Guessing an API, or reporting
a sandbox result as a real order, would mislead users and could spend their money. Comparison/review
services (Skyscanner, Tripadvisor) are marked `compare` so they're never presented as booking channels.

## ADR-015 — Payments: approval first, gateway link second, provider proof last (2026-11-14)

**Decision:**
1. The user approves the exact purchase. The action hash covers amount, items, address and payment
   method.
2. Apna creates a Razorpay payment link keyed by the step's idempotency key.
3. The user pays in their own UPI app.
4. Completion is accepted only from a signature-verified webhook or the gateway's status API.
   `PaymentWorkflow` waits for the webhook and polls the status API, then completes or fails the
   purchase exactly once.

Webhook event ids are stored for deduplication. The payment step is deliberately *not* treated as
idempotent: after an interrupted attempt the outcome is "uncertain" and must be reconciled, never blindly
retried. If the quote has expired and the price changed, the old approval is void.

**Why:** a payment link or a screenshot isn't proof of payment. Webhooks can be lost or duplicated, and
workers can crash between charging and recording.

## ADR-016 — Secrets never travel through chat (2026-11-14)

**Decision:**
- **Encryption at rest:** provider tokens and login codes are AES-256-GCM encrypted with associated data
  bound to the owner (user + provider, or the challenge). The key comes from `APNA_ENCRYPTION_KEY`, with
  no default.
- **Login codes:** asked for only after a real provider challenge, on a one-time page served by the
  public webhook-only instance. They're consumed once and wiped, and never reach the planner, logs or
  workflow history.
- **Never collected:** payment secrets (UPI PIN, card PIN, CVV, bank passwords, payment OTPs). Challenges
  whose purpose mentions payment are refused.
- **Chat redaction:** anything that looks like a card number, CVV, PIN, OTP or password is masked before
  storage and planning, and the user is warned.

## ADR-017 — One offers engine for every "find the cheapest" request (2026-11-16)

**Decision:** products, food, flights, hotels, buses, events, tables and ride fares share one pipeline:
`search_offers` (read-only) → user picks → `book_offer` (re-quotes, holds 15 min) → `pay_offer`
(exact-amount approval, single use) → TEST confirmation or Razorpay link. Comparison is per **identical
item** (same model / dish+restaurant / flight / hotel+dates / bus / event tier) and ranks by the **full
total**, never the headline price. Tables rank by discount and are confirmed without payment.
**Why:** one approval and payment path to keep safe instead of eight. Picking from a list is resolved in
code ("1", "sabse sasta", "Flipkart wala" → "book offer OFR-…") and planned without a model, so a free-tier
model can't book the wrong option.
**Trade-off:** prices are TEST data until partner APIs exist; every reply says so and links the real site.

## ADR-018 — Web results are shown, not believed (2026-11-16)

**Decision:** `web_search` returns titles, sources, dates and links that the reply renders directly. They
never go back to the planner. Tavily's own summary is shown labelled as Tavily's. IPO answers link NSE/BSE
and say live subscription/GMP data needs exchange licensing.
**Why:** web pages are untrusted (prompt injection, stale or wrong numbers).

## ADR-019 — Google access is per user, by OAuth with PKCE (2026-11-16)

**Decision:** "connect Google" returns Google's consent URL with a single-use, hashed, 10-minute state and a
PKCE S256 challenge (verifier encrypted). The callback on the public instance exchanges the code; tokens are
stored encrypted (AAD = user + provider). Drafts stay in Apna's table and sending rebuilds the message
from the approved draft. Calendar events have no invitees and a deterministic id (retries can't
duplicate).
**Why:** Apna never sees Google passwords or 2-step codes. Approvals bind exactly what is sent or written.
