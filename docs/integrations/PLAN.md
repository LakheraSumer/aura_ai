# Commerce / travel / services extension: plan (2026-11-14)

This plan is repository-specific. Evidence is in [CAPABILITY_MATRIX.md](CAPABILITY_MATRIX.md).

**Finding that shapes everything:** none of the requested grocery, ride, home-services or travel
providers offers self-serve ordering or booking. Swiggy (Builders Club), Zomato (MCP), Uber
(Ride Requests), Booking.com (Demand API) and Skyscanner need partner approval. Blinkit, Zepto,
BigBasket, Flipkart Minutes, Rapido, Urban Company, MakeMyTrip, Agoda and redBus have no public ordering
API. So:

- **Live:** weather (Open-Meteo), verified handoff links, and payments once the owner adds Razorpay test
  keys.
- **Sandbox:** the full transaction conversation (quote → cart → approval → payment → confirmation →
  status), clearly labelled TEST.
- **Disabled with an honest reason:** every partner-only operation, until an adapter is written against
  that provider's real docs.

## Build order (as implemented)

A. **Shared layer**
   - `providers/types.py`: normalised money, price breakdown, quote, cart and booking reference.
   - `providers/registry.py`: per-provider, per-operation capability, credentials detection,
     `require()` enforcement, handoff links.
   - Migration `0005`: provider accounts (encrypted tokens), auth challenges, payment intents, webhook
     dedupe, cart quote expiry.
   - Payment workflow: payment pending → paid/failed/expired, with reconciliation polling.

B. **Weather** (`providers/weather.py`, live) and the existing sandbox commerce, now registered.

C. **Grocery**
   - No provider grants access, so the sandbox implements the acceptance flow: equivalent pack sizes,
     store subset ("Blinkit ya Zepto"), cheapest-total choice, variant questions, no silent
     substitution, serviceability and minimum order, quote expiry and re-approval on price change,
     cart edits, order status.
   - Plus a verified Blinkit search link for the real app.

D. **Everything else**
   - Food, rides, home services, travel, buses and payments are registered, with handoff links and
     partner requirements explained per operation.
   - Razorpay payment links and webhook adapter (credentials-required).

## Security pieces

- AES-GCM encryption for provider tokens and OTPs (`security/crypto.py`). The key comes from
  `APNA_ENCRYPTION_KEY`, with no built-in default.
- One-time OTP entry page bound to user + provider + purpose, 5-minute TTL, 3 attempts. Created only
  from a provider adapter's challenge. The OTP never enters chat, the LLM, logs or workflow history.
- Chat redaction: card numbers, CVV/PIN/OTP-looking messages are masked before they're stored or sent
  to the model, and the user is warned.
