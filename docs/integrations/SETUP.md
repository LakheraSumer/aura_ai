# Integrations: setup and partner access

What works out of the box, what needs keys, and what needs a partner agreement. Status per provider and
operation: [CAPABILITY_MATRIX.md](CAPABILITY_MATRIX.md). Never put secrets in chat or in git: use
`scripts/set_secret.ps1 NAME` (hidden input) to write them into `services/api/.env`.

## Works now (no keys)

| Feature | Notes |
|---|---|
| Weather (Open-Meteo) | Live. Free for **non-commercial** use only; a commercial launch needs an Open-Meteo subscription. |
| Grocery comparison, carts, edits, status | **TEST sandbox** modelled on Blinkit / Zepto / Instamart / BB Now / Flipkart Minutes. Every reply says TEST and links the real app. |
| Movie booking flow | TEST sandbox cinema + real BookMyShow showtime links. |
| Rides, food, hotels, flights, buses, home services | Honest per-app explanation + verified app/site links (Uber universal link, Booking.com search, redBus route, Blinkit search; homepages elsewhere). |
| Chat redaction | Card numbers, CVV, PIN, OTP and passwords are masked before storage and the planner. |
| Price comparison + booking | **TEST** offers engine: products, food, flights, hotels, buses, events, tables (by discount), ride fares. Same item at every source, total incl. fees/taxes, cheapest first, savings shown; pick a number → hold → approval → TEST booking. Real-site links in every reply. |
| Web search, news, IPOs | Live: Google News RSS + Wikipedia. Add Tavily (below) for better general answers. |
| Price-drop alerts, shopping list, reminder Done/Snooze follow-ups | Work now (alerts use TEST prices). |

## Needs a key you can create yourself

1. **Encryption key** (required for account linking and the secure code page)
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\set_secret.ps1 APNA_ENCRYPTION_KEY
   ```
   Paste a key from `python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.
2. **Razorpay (real payments in chat via UPI: GPay / PhonePe / Paytm)**
   - Create a Razorpay account and switch the dashboard to **Test mode**.
   - Generate API keys (`rzp_test_…`) and save them with `set_secret.ps1`: `RAZORPAY_KEY_ID`,
     `RAZORPAY_KEY_SECRET`.
   - Add a webhook at `<public URL>/v1/webhooks/razorpay` with events `payment_link.paid`,
     `payment_link.expired` and `payment_link.cancelled`. Set its secret, then save it as
     `RAZORPAY_WEBHOOK_SECRET`.
   - Recreate the containers (`scripts\docker_stack.ps1 up -Dev`). Grocery payments then go through a
     real Razorpay test link, and an order is placed only after Razorpay confirms (signed webhook or status
     API).
   - Live mode needs Razorpay KYC for a business, plus your approval for real money.
3. **Tripadvisor Content API**: key with a card on file (5,000 free calls/month per their FAQ, possibly
   moving to "Terra" with 1,000 free). The adapter is not built yet.

4. **Tavily web search (optional, free 1,000 credits/month, no card)**: sign up at tavily.com, then
   `set_secret.ps1 TAVILY_API_KEY` and recreate the containers. Without it Apna uses Google News + Wikipedia.
5. **Google (Gmail + Calendar)**
   - You need a **fixed** public HTTPS URL (a named Cloudflare tunnel or your domain). The quick
     `trycloudflare.com` URL changes on every restart, and Google only accepts registered redirect URIs.
   - Google Cloud Console → new project → enable **Gmail API** and **Google Calendar API**.
   - OAuth consent screen: External, add yourself under **Test users**. Scopes: `gmail.readonly`,
     `gmail.send`, `calendar.events`. (Public launch needs Google's verification for Gmail scopes.)
   - Credentials → OAuth client ID → **Web application**; authorised redirect URI:
     `<PUBLIC_BASE_URL>/v1/oauth/google/callback`.
   - Save `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (hidden input) and set `PUBLIC_BASE_URL`, then
     `scripts\docker_stack.ps1 up -Dev`. In chat say "connect Google" and sign in on Google's page.

## Needs a partner agreement (apply; Apna can't self-serve these)

| Provider | Program | What it would unlock |
|---|---|---|
| Swiggy Food + Instamart | Builders Club, https://mcp.swiggy.com/builders (apply, review; OAuth 2.1 + PKCE) | Real search, carts, checkout and tracking for Swiggy food and Instamart groceries |
| Zomato | Zomato MCP access request (OAuth redirect URI whitelisted by Zomato) | Restaurant search, cart, checkout |
| Uber | Ride Requests API approval via Uber business development | Fare estimates, ride booking, status, cancel |
| Booking.com | Managed Affiliate Partner contract → Demand API | Hotel search and booking |
| Skyscanner | Travel API (established business, >100k monthly traffic) | Flight price comparison |
| Trip.com | B2B / affiliate partnership | Search and booking |
| PhonePe PG | Merchant onboarding (UAT sandbox → production) | PhonePe Standard Checkout as a second gateway |
| Blinkit, Zepto, BigBasket, Flipkart Minutes, Rapido, Urban Company, MakeMyTrip, Agoda, redBus | No public ordering/booking API found | Handoff links only, unless a partnership is negotiated (or ONDC as a registered buyer app for groceries) |

When access is granted, write an adapter against that provider's **real** documentation. Then change its
registry entry (`services/api/src/apna/providers/registry.py`) from `partner-access-required` to the
real status, and add tests with recorded fixtures. Never enable an operation before its adapter
exists.

## WhatsApp

The WhatsApp Cloud API adapter stays gated (`WHATSAPP_PRODUCTION_ENABLED=false`). Meta's terms bar
general-purpose AI assistants (since 2026-01-15), and the owner's WhatsApp Business Account is currently
restricted pending business verification. Telegram is the working channel.
