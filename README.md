# Apna AI

**Your personal AI assistant. Just ask, and it gets things done.** India-first, multilingual
(English, Hindi, Marathi, Hinglish), and chat-only: Telegram today, WhatsApp in test mode
(see [WhatsApp compliance](docs/compliance/WHATSAPP_COMPLIANCE.md)).

```
WhatsApp / Telegram ──webhook──▶ FastAPI ──▶ ConversationService ──start──▶ Temporal TaskWorkflow
                                                                     │
          plan (Claude or rule planner) ─▶ ground (dates, validation, risk) ─▶ steps
          policy engine ─▶ approval (exact-action hash) ─▶ executor ─▶ verify ─▶ reply from verified state
```

Status of every capability, with evidence: [docs/DEVELOPMENT_PROGRESS.md](docs/DEVELOPMENT_PROGRESS.md).
Design decisions: [docs/ARCHITECTURE_DECISIONS.md](docs/ARCHITECTURE_DECISIONS.md).

## Run it locally (Windows 11, PowerShell)

Prerequisites: Docker Desktop, Python 3.12.

```powershell
# 1. Infrastructure: Postgres+pgvector :55432, Redis :56379, Temporal :57233 (UI http://localhost:58233)
docker compose -f infrastructure/docker/docker-compose.yml up -d

# 2. Backend
cd services/api
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m playwright install chromium
copy ..\..\.env.example .env
.venv\Scripts\python -m alembic upgrade head

# 3. Processes (three terminals)
.venv\Scripts\python -m apna.workflows.worker --all          # Temporal worker: tasks + browser queue
.venv\Scripts\python -m uvicorn apna.api.main:app --port 8800 # API, docs at http://127.0.0.1:8800/docs
.venv\Scripts\python -m apna.browser.testsite                 # sandbox website for browser tasks

# 4. Try it
.venv\Scripts\python ..\..\scripts\smoke.py
```

Tests (run from the repo root; the integration tests need step 1 running):

```powershell
services\api\.venv\Scripts\python -m pytest tests            # 152 tests
```

> If the repo lives under a very long Windows path, Playwright and SQLAlchemy fail to load native
> files. Use a short checkout path (see docs/OPEN_ISSUES.md #5).

## Commerce, travel and services (2026-11-14)

- Groceries: compares Blinkit / Zepto / Instamart / BB Now / Flipkart Minutes-style stores on equivalent
  quantities incl. fees, asks which product variant, builds and edits carts, takes an exact-amount
  approval, and pays (TEST sandbox, or a real Razorpay UPI link once you add test keys). **The real apps
  have no ordering API**, so orders are TEST; replies link the real app.
- Live weather (Open-Meteo). Rides, food, hotels, flights, buses and home services: honest per-app limits
  plus verified links (Uber with pickup/drop, Booking.com with dates, redBus route).
- **Compare and book (2026-11-16):** the same product or service priced at every source, cheapest total
  first, with the saving: products (Amazon, Flipkart, Croma...), food (Swiggy vs Zomato), flights, hotels,
  buses, events, restaurant tables (best discount) and ride fares. Reply with a number → hold → approval →
  TEST booking. Also: price-drop alerts, shopping lists ("order my list"), live web search (news, IPOs),
  reminder Done/Snooze follow-ups, and Gmail/Calendar after "connect Google" (needs Google keys).
- What each provider allows, with evidence: [docs/integrations/CAPABILITY_MATRIX.md](docs/integrations/CAPABILITY_MATRIX.md).
  Keys and partner programs: [docs/integrations/SETUP.md](docs/integrations/SETUP.md).

Run the full test suite in a container (works even where Windows blocks native modules):

```powershell
docker run --rm -u root --network apna-ai_default -e IT_PG=postgres:5432 -e IT_TEMPORAL=temporal:7233 `
  -v "${PWD}:/repo" -v "${PWD}\services\api\src\apna:/usr/local/lib/python3.12/site-packages/apna:ro" `
  apna-backend:dev sh -c "pip install -q pytest pytest-asyncio && cd /repo && python -m pytest tests -q"
```

## What works today (mock / sandbox, no paid credentials)

- Reminders in Hinglish, Hindi or Marathi, resolved in IST. It asks instead of guessing ("kal" in a
  past-tense sentence, "next Friday" within the same week, "8 baje" with no AM/PM).
- Email reply drafts from a sandbox mailbox. Sending always needs an approval button, and the approval
  is bound to the exact recipient and content.
- Train lookups that return station codes and the official IRCTC link. It never makes up trains,
  fares or seats.
- Isolated browser tasks on an allow-listed sandbox site: screenshots, a redacted action log,
  confirmation checked against the site's own record, prompt-injection pages ignored, and a handoff to
  you on password or OTP pages.
- Durable execution: a worker crash while waiting for approval resumes without a duplicate send.
- WhatsApp and Telegram adapters with signed webhooks, button approvals, cross-chat linking
  (`/linkcode`, then `link CODE`), and WhatsApp's 24-hour window and production gate.

## Choosing the AI planner

`PLANNER_PROVIDER` in `services/api/.env`: `anthropic` (Claude `claude-opus-5-5`), `openai`
(`gpt-5.4-mini`), `gemini` (free tier, `gemini-3.1-flash-lite`; test data only), `rule` (offline), or
`auto`. Set keys with `scripts/set_secret.ps1 GEMINI_API_KEY` (hidden input) rather than pasting them
anywhere. If the provider fails (no credit, outage), that task falls back to the offline rule planner.

## Run as containers (recommended on this PC)

```powershell
docker compose -f infrastructure/docker/docker-compose.yml up -d
docker build -f infrastructure/docker/backend.Dockerfile --target base -t apna-backend:dev .
powershell -ExecutionPolicy Bypass -File scripts\docker_stack.ps1 up -Dev   # api :8800 (local only), worker, webhook-only instance
powershell -ExecutionPolicy Bypass -File scripts\docker_stack.ps1 tunnel    # public HTTPS URL for Meta/Telegram webhooks
```

The tunnel reaches only the webhook-only instance. After editing `.env`, run `up` again; the
containers read `.env` only when they're created.
