# Provider capability matrix

Verified **2026-11-14** from official docs where they exist, plus a real-browser check of every handoff
link. The code registry is `services/api/src/apna/providers/registry.py`; this file must stay in sync with it.

**Statuses** (per operation):
- `live`: works now against the real provider.
- `sandbox`: Apna's TEST provider; nothing real happens, and replies say TEST.
- `credentials-required`: an official self-serve API exists; it becomes `live` once keys are set in `.env`.
- `partner-access-required`: an official API exists, but only after a partnership, contract or review.
- `handoff-only`: Apna can only open the provider's own app or site for the user.
- `unsupported`: not available in any form.

**Kinds:**
- `transact`: the provider can take an order or booking.
- `compare`: comparison or review only; it never takes the booking itself.
- `payment`: a payment gateway.
- `info`: information only.

## Summary

| Provider | Domain | Kind | Official integration (evidence) | What Apna can do today |
|---|---|---|---|---|
| Blinkit | grocery | transact | No public ordering API found; only third-party scrapers and brand-side integrations ([search 2026-11-14](https://www.fynd.com/blog/quick-commerce-order-management-how-brands-can-automate-blinkit-zepto-and-instamart-fulfillment)) | **handoff-only** (verified search link `blinkit.com/s/?q=`); full flow in **sandbox** |
| Zepto | grocery | transact | No official API. A Zepto employee's MCP drives a browser (personal project, unsupported) ([coverage](https://www.cxodigitalpulse.com/?p=41815)) | **handoff-only** (homepage; search link redirects); **sandbox** flow |
| Swiggy Instamart | grocery | transact | **Swiggy Builders Club**: MCP servers + APIs, OAuth 2.1 + PKCE, invite/application then review ([docs](https://mcp.swiggy.com/builders/docs/start/what-is-swiggy-mcp/), [launch](https://mcp.swiggy.com/builders/blog/2026-04-17-builders-club-launch/)) | **partner-access-required**; handoff (homepage); **sandbox** flow |
| BigBasket (BB Now) | grocery | transact | No public API (partner portal is for suppliers) ([partner login](https://www.bigbasket.com/partner/login/)) | **handoff-only** (homepage); **sandbox** flow |
| Flipkart Minutes | grocery | transact | Only Seller and Affiliate APIs, no ordering API ([seller API](https://seller.flipkart.com/api-docs), [affiliate](https://affiliate.flipkart.com/api-docs/affiliate_index.html)) | **handoff-only**; **sandbox** flow |
| Swiggy Food | food | transact | Builders Club Food MCP: discovery, menus, ordering, tracking ([docs](https://mcp.swiggy.com/builders/docs/start/what-is-swiggy-mcp/)) | **partner-access-required**; handoff |
| Zomato | food | transact | Official MCP server, gated: request form, whitelisted OAuth redirect URIs, QR payment ([Claude connector](https://claude.com/connectors/zomato), [overview](https://www.f22labs.com/blogs/zomato-mcp-server-guide-architecture-and-features/)) | **partner-access-required**; handoff |
| Razorpay | payment | payment | Payment Links / UPI with test mode; webhooks signed with HMAC-SHA256 in `X-Razorpay-Signature`, deduplicated with `x-razorpay-event-id` ([webhooks](https://razorpay.com/docs/webhooks/validate-test)) | **credentials-required** (adapter built: links, status, webhook); otherwise sandbox payment |
| PhonePe PG | payment | payment | Standard Checkout v2: OAuth token, `/checkout/v2/pay`, order status; UAT sandbox with merchant credentials ([UAT](https://developer.phonepe.com/payment-gateway/uat-testing-go-live/uat-sandbox)) | **credentials-required** (registered; adapter not built yet) |
| Google Pay (India) | payment | payment | UPI with a merchant VPA, NPCI/bank-verified business, UPI intent, or through a gateway ([Google Pay India API](https://developers.google.com/pay/india/api/android/overview), [via Razorpay](https://razorpay.com/docs/payments/payment-methods/upi/google-pay/omnichannel)) | Used **through a gateway** (Razorpay UPI link opens GPay/PhonePe on the phone); no wallet access |
| Uber | ride | transact | Ride Requests API **needs Uber approval**; deep links are open ([riders intro](https://developer.uber.com/docs/riders/introduction), [deep links](https://developer.uber.com/docs/riders/ride-requests/tutorials/deep-links/faq)) | **handoff-only** (universal link `m.uber.com/ul/?action=setPickup…`, verified); API **partner-access-required** |
| Rapido | ride | transact | No public API; community SDKs are reverse-engineered (excluded) | **handoff-only** (homepage) |
| Urban Company | home | transact | No public API found | **handoff-only** (homepage) |
| Skyscanner | travel | compare | Travel API for approved partners only (≥100k monthly traffic) ([criteria](https://skyscannerpartnersupport.zendesk.com/hc/en-us/articles/10881149122717-What-is-the-acceptance-criteria-for-the-Travel-API)) | **partner-access-required**; handoff (homepage). Search links hit a **CAPTCHA** in the browser check, so they're not used |
| Booking.com | travel | transact | Demand API needs a Managed Affiliate Partner contract ([prerequisites](https://developers.booking.com/demand/docs/getting-started/prerequisites)) | **partner-access-required**; **handoff-only** search link (verified) |
| Agoda | travel | transact | Affiliate program only; no API evidence found | **handoff-only** (homepage) |
| Trip.com | travel | transact | Affiliate links, plus B2B API for qualified partners ([overview](https://www.adivaha.com/ctrip-s-top-travel-picks-for-your-next-adventure.html)) | **partner-access-required**; handoff (homepage) |
| Tripadvisor | travel | compare | Content API: key + card, 5,000 free calls/month per official FAQ, possibly moving to "Terra" (1,000 free) ([FAQ](https://tripadvisor-content-api.readme.io/reference/faq), [Terra](https://docs.terra.tripadvisor.com/docs/authenticate-your-application)) | **credentials-required** (reviews/ratings only; adapter not built yet); handoff search |
| MakeMyTrip | travel | transact | Affiliate only; no public API found | **handoff-only** (homepage) |
| redBus | bus | transact | Affiliate program; no public API found | **handoff-only** (route link `redbus.in/bus-tickets/{from}-to-{to}`, verified) |
| Open-Meteo | weather | info | Free forecast + geocoding API, no key; **non-commercial use only**, commercial needs a subscription ([terms](https://open-meteo.com/en/terms), [pricing](https://open-meteo.com/en/pricing)) | **live** (non-commercial) |
| Sandbox grocery / cinema | grocery, movies | transact | Apna's own TEST providers | **sandbox**: search, quote, cart, update cart, checkout summary, pay (TEST), status |

## Operations by provider

The registry encodes the same information per operation. Operation names: `search`, `get_details`,
`check_serviceability`, `get_quote`, `create_cart`, `update_cart`, `get_checkout_summary`,
`initiate_checkout`, `create_booking`, `get_status`, `cancel`, `get_refund_status`, `handoff`.
For every real provider above, any operation not listed in the registry is `unsupported`. With partner
access, the partner-API operations become available only after an adapter is written against the
provider's real docs. Apna never guesses an API.

## WhatsApp channel

- Meta's WhatsApp Business Solution terms bar general-purpose AI assistants (effective 2026-01-15). Task
  bots for bookings, order tracking and support are allowed. No later relaxation was found
  ([TechCrunch](https://techcrunch.com/2025/10/18/whatssapp-changes-its-terms-to-bar-general-purpose-chatbots-from-its-platform/)).
- The owner's WhatsApp Business Account is **restricted until business verification** (dashboard,
  2026-11-14). The production gate (`WHATSAPP_PRODUCTION_ENABLED=false`) and test-number allow-list stay as
  they are. Apna never uses unofficial WhatsApp clients.
- Telegram remains the working live channel.

## What would make each provider real

| To get… | You need |
|---|---|
| Real grocery and food orders | Swiggy Builders Club approval (Instamart + Food), Zomato MCP access, or ONDC buyer-app registration (registered business) |
| Real payments in chat | Razorpay account → test keys (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`) → KYC for live; or PhonePe PG merchant onboarding |
| Ride booking via API | Uber business-development approval for the Ride Requests API |
| Hotel/flight booking via API | Booking.com Managed Affiliate contract; Skyscanner/Trip.com partner approval |
| Reviews | Tripadvisor Content API key (card on file) |
| Commercial weather | Open-Meteo commercial subscription |

## Added 2026-11-16: offers engine sources, web search, Google

Comparison prices and bookings for these sources are **sandbox** (offers engine, TEST). The real sites:

| Provider | Real status | Why | Link Apna gives (browser-checked 2026-11-16) |
|---|---|---|---|
| Amazon | partner-access-required (search/quotes), handoff-only | Product Advertising API 5.0 needs an Associates account with qualifying sales; no ordering API | `amazon.in/s?k=<product>` |
| Flipkart | partner-access-required (affiliate), handoff-only | Affiliate API for approved affiliates; no ordering API | `flipkart.com/search?q=<product>` |
| Croma, Reliance Digital, Myntra | handoff-only | No public API | homepages |
| Swiggy Dineout | partner-access-required | Swiggy partner program | `swiggy.com/dineout` |
| District (by Zomato) | handoff-only | No public API | `district.in` |
| BookMyShow (events) | handoff-only | No public booking API | `in.bookmyshow.com/explore/events-<city>` |
| Ola | handoff-only | Partner API closed to new integrators | `olacabs.com` |
| Airlines (IndiGo, Air India, Akasa, Air India Express, SpiceJet) | handoff-only | No public booking API for third parties | each airline's own site |
| MakeMyTrip / Trip.com / Booking.com flights | handoff-only / partner | see travel rows above | `makemytrip.com/flights/`, `trip.com/flights/`, `booking.com/flights/` |
| Google News RSS | live | Free; headlines + links | `news.google.com/rss/search?...&gl=IN` |
| Wikipedia | live | REST summary + search API | `en.wikipedia.org` |
| Tavily | credentials-required (adapter built) | `TAVILY_API_KEY` (free 1,000 credits/month) | — |
| Google (Gmail + Calendar) | credentials-required (adapter built) | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, fixed `PUBLIC_BASE_URL`; each user connects by OAuth. Gmail scopes need Google verification before public launch | `accounts.google.com` consent page |
