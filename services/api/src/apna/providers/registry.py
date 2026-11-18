"""Capability registry: what Apna can really do with each provider, per operation.

Source of truth for capability enforcement. Mirrors docs/integrations/CAPABILITY_MATRIX.md (verified
2026-11-14). Rules:
- An operation not listed for a provider is UNSUPPORTED.
- CREDENTIALS_REQUIRED becomes LIVE only when every listed env credential is set AND an adapter exists
  (`adapter_built`). Missing credentials never fall back to fabricated success.
- PARTNER_ACCESS_REQUIRED stays disabled until an adapter is written against the provider's real docs
  after access is granted. Apna never guesses an API.
- Handoff links were each opened in a real browser on 2026-11-14; links that redirected away from the
  intended page (or hit a CAPTCHA) fall back to the provider's homepage.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from apna.core.errors import ApnaError
from apna.providers.types import CapabilityStatus as S
from apna.providers.types import Operation as Op

VERIFIED_ON = "2026-11-14"


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    domain: str  # grocery | food | ride | home | travel | bus | payment | weather | movies
    kind: str  # transact | compare | payment | info
    ops: Mapping[Op, S]
    homepage: str
    evidence: tuple[str, ...]
    credentials: tuple[str, ...] = ()  # env var names
    adapter_built: bool = False
    partner_program: str | None = None
    handoff: Callable[[dict[str, Any]], str] | None = None
    notes: str = ""
    sandbox_of: str | None = None  # for sandbox providers: which real provider it imitates
    extra: dict[str, Any] = field(default_factory=dict)


class CapabilityUnavailable(ApnaError):
    code = "capability_unavailable"
    http_status = 409


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _blinkit(p: dict[str, Any]) -> str:  # verified: "Buy milk Online | blinkit"
    q = p.get("query")
    return f"https://blinkit.com/s/?q={quote(str(q))}" if q else "https://blinkit.com/"


def _uber(p: dict[str, Any]) -> str:  # documented universal link (developer.uber.com deep links)
    params: dict[str, str] = {"action": "setPickup"}
    params["pickup"] = "my_location" if not p.get("pickup") else ""
    if p.get("pickup"):
        params.pop("pickup")
        params["pickup[formatted_address]"] = str(p["pickup"])
    dropoff = p.get("dropoff") or p.get("destination")
    if dropoff:
        params["dropoff[formatted_address]"] = str(dropoff)
    return "https://m.uber.com/ul/?" + urlencode(params)


def _booking(p: dict[str, Any]) -> str:  # verified: "Booking.com: Search results: Goa"
    params = {"ss": p.get("destination") or p.get("query") or ""}
    for k_in, k_out in (("checkin", "checkin"), ("checkout", "checkout")):
        if p.get(k_in):
            params[k_out] = str(p[k_in])
    params["group_adults"] = str(p.get("adults") or 2)
    params["no_rooms"] = str(p.get("rooms") or 1)
    return "https://www.booking.com/searchresults.html?" + urlencode(params)


def _redbus(p: dict[str, Any]) -> str:  # verified: "Pune to Mumbai Bus - Book from 744 Buses"
    if p.get("origin") and p.get("destination"):
        return f"https://www.redbus.in/bus-tickets/{_slug(p['origin'])}-to-{_slug(p['destination'])}"
    return "https://www.redbus.in/"


def _ops(**kw: S) -> dict[Op, S]:
    return {Op(k): v for k, v in kw.items()}


_HANDOFF = _ops(handoff=S.HANDOFF_ONLY)
_SANDBOX_COMMERCE = _ops(search=S.SANDBOX, check_serviceability=S.SANDBOX, get_quote=S.SANDBOX,
                         create_cart=S.SANDBOX, update_cart=S.SANDBOX, get_checkout_summary=S.SANDBOX,
                         initiate_checkout=S.SANDBOX, get_status=S.SANDBOX, cancel=S.SANDBOX)
_SWIGGY_PARTNER = {**_HANDOFF, **_ops(search=S.PARTNER_ACCESS_REQUIRED, get_quote=S.PARTNER_ACCESS_REQUIRED,
                                      create_cart=S.PARTNER_ACCESS_REQUIRED, update_cart=S.PARTNER_ACCESS_REQUIRED,
                                      initiate_checkout=S.PARTNER_ACCESS_REQUIRED,
                                      get_status=S.PARTNER_ACCESS_REQUIRED)}

_SPECS = [
    # ------------------------------------------------------------------ grocery
    ProviderSpec("blinkit", "Blinkit", "grocery", "transact", _HANDOFF, "https://blinkit.com/",
                 ("no public ordering API found (search 2026-11-14)",), handoff=_blinkit,
                 notes="Blinkit doesn't offer ordering to outside apps."),
    ProviderSpec("zepto", "Zepto", "grocery", "transact", _HANDOFF, "https://www.zepto.com/",
                 ("no official API; an employee's browser-driven MCP is a personal project",),
                 notes="Zepto doesn't offer ordering to outside apps."),
    ProviderSpec("instamart", "Swiggy Instamart", "grocery", "transact", _SWIGGY_PARTNER,
                 "https://www.swiggy.com/instamart",
                 ("https://mcp.swiggy.com/builders/docs/start/what-is-swiggy-mcp/",),
                 partner_program="Swiggy Builders Club (apply at https://mcp.swiggy.com/builders, OAuth 2.1 + PKCE)",
                 notes="Ordering needs Swiggy Builders Club approval."),
    ProviderSpec("bigbasket", "BB Now (BigBasket)", "grocery", "transact", _HANDOFF, "https://www.bigbasket.com/",
                 ("partner portal is for suppliers only",), notes="BigBasket doesn't offer ordering to outside apps."),
    ProviderSpec("flipkart_minutes", "Flipkart Minutes", "grocery", "transact", _HANDOFF, "https://www.flipkart.com/",
                 ("https://seller.flipkart.com/api-docs", "https://affiliate.flipkart.com/api-docs/affiliate_index.html"),
                 notes="Flipkart only offers seller and affiliate APIs, not ordering."),
    # ------------------------------------------------------------------ food
    ProviderSpec("swiggy_food", "Swiggy", "food", "transact", _SWIGGY_PARTNER, "https://www.swiggy.com/",
                 ("https://mcp.swiggy.com/builders/docs/start/what-is-swiggy-mcp/",),
                 partner_program="Swiggy Builders Club (Food MCP)", notes="Ordering needs Swiggy Builders Club approval."),
    ProviderSpec("zomato", "Zomato", "food", "transact",
                 {**_HANDOFF, **_ops(search=S.PARTNER_ACCESS_REQUIRED, create_cart=S.PARTNER_ACCESS_REQUIRED,
                                     initiate_checkout=S.PARTNER_ACCESS_REQUIRED)},
                 "https://www.zomato.com/", ("https://claude.com/connectors/zomato",),
                 partner_program="Zomato MCP (request form; OAuth redirect URI must be whitelisted by Zomato)",
                 notes="Zomato's ordering server is gated; access is by request."),
    # ------------------------------------------------------------------ payments
    ProviderSpec("razorpay", "Razorpay (UPI / GPay / PhonePe apps)", "payment", "payment",
                 _ops(initiate_checkout=S.CREDENTIALS_REQUIRED, get_status=S.CREDENTIALS_REQUIRED),
                 "https://razorpay.com/", ("https://razorpay.com/docs/webhooks/validate-test",),
                 credentials=("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"), adapter_built=True,
                 notes="Payment links: the user pays in their own UPI app; Apna never sees a PIN."),
    ProviderSpec("phonepe_pg", "PhonePe Payment Gateway", "payment", "payment",
                 _ops(initiate_checkout=S.CREDENTIALS_REQUIRED, get_status=S.CREDENTIALS_REQUIRED),
                 "https://www.phonepe.com/business-solutions/payment-gateway/",
                 ("https://developer.phonepe.com/payment-gateway/uat-testing-go-live/uat-sandbox",),
                 credentials=("PHONEPE_CLIENT_ID", "PHONEPE_CLIENT_SECRET", "PHONEPE_CLIENT_VERSION"),
                 adapter_built=False, notes="Standard Checkout v2 (merchant onboarding); adapter not built yet."),
    ProviderSpec("google_pay", "Google Pay (UPI)", "payment", "payment", {}, "https://pay.google.com/",
                 ("https://developers.google.com/pay/india/api/android/overview",),
                 notes="Used through a gateway's UPI flow; Apna has no access to users' Google Pay accounts."),
    # ------------------------------------------------------------------ rides
    ProviderSpec("uber", "Uber", "ride", "transact",
                 {**_HANDOFF, **_ops(get_quote=S.PARTNER_ACCESS_REQUIRED, create_booking=S.PARTNER_ACCESS_REQUIRED,
                                     get_status=S.PARTNER_ACCESS_REQUIRED, cancel=S.PARTNER_ACCESS_REQUIRED)},
                 "https://www.uber.com/", ("https://developer.uber.com/docs/riders/introduction",
                                           "https://developer.uber.com/docs/riders/ride-requests/tutorials/deep-links/faq"),
                 partner_program="Uber Ride Requests API (approval by Uber business development)", handoff=_uber,
                 notes="Booking rides through Uber's API needs Uber's approval; the Uber app link is open."),
    ProviderSpec("rapido", "Rapido", "ride", "transact", _HANDOFF, "https://www.rapido.bike/",
                 ("no public API; community SDKs are reverse-engineered and excluded",),
                 notes="Rapido has no public booking API."),
    # ------------------------------------------------------------------ home services
    ProviderSpec("urban_company", "Urban Company", "home", "transact", _HANDOFF, "https://www.urbancompany.com/",
                 ("no public API found",), notes="Urban Company has no public booking API."),
    # ------------------------------------------------------------------ travel
    ProviderSpec("skyscanner", "Skyscanner", "travel", "compare",
                 {**_HANDOFF, **_ops(search=S.PARTNER_ACCESS_REQUIRED, get_quote=S.PARTNER_ACCESS_REQUIRED)},
                 "https://www.skyscanner.co.in/",
                 ("https://skyscannerpartnersupport.zendesk.com/hc/en-us/articles/10881149122717",),
                 partner_program="Skyscanner Travel API (established business, >100k monthly traffic)",
                 # Its search deep links hit a CAPTCHA in the 2026-11-14 browser check: homepage only.
                 notes="Skyscanner compares fares; it doesn't take bookings."),
    ProviderSpec("booking_com", "Booking.com", "travel", "transact",
                 {**_HANDOFF, **_ops(search=S.PARTNER_ACCESS_REQUIRED, create_booking=S.PARTNER_ACCESS_REQUIRED)},
                 "https://www.booking.com/", ("https://developers.booking.com/demand/docs/getting-started/prerequisites",),
                 partner_program="Booking.com Demand API (Managed Affiliate Partner contract)", handoff=_booking,
                 notes="Hotel search through the API needs a Booking.com partner contract."),
    ProviderSpec("agoda", "Agoda", "travel", "transact", _HANDOFF, "https://www.agoda.com/",
                 ("affiliate program only",), notes="Agoda offers affiliate links, not a booking API."),
    ProviderSpec("tripcom", "Trip.com", "travel", "transact",
                 {**_HANDOFF, **_ops(search=S.PARTNER_ACCESS_REQUIRED)}, "https://www.trip.com/",
                 ("affiliate + B2B partner API",), partner_program="Trip.com B2B/affiliate partnership",
                 notes="Trip.com's API is for approved partners."),
    ProviderSpec("tripadvisor", "Tripadvisor", "travel", "compare",
                 {**_HANDOFF, **_ops(search=S.CREDENTIALS_REQUIRED, get_details=S.CREDENTIALS_REQUIRED)},
                 "https://www.tripadvisor.in/", ("https://tripadvisor-content-api.readme.io/reference/faq",),
                 credentials=("TRIPADVISOR_API_KEY",), adapter_built=False,
                 notes="Reviews and ratings only; it doesn't take bookings."),  # Content API adapter not built
    ProviderSpec("makemytrip", "MakeMyTrip", "travel", "transact", _HANDOFF, "https://www.makemytrip.com/",
                 ("affiliate only; no public API found",), notes="MakeMyTrip has no public booking API."),
    ProviderSpec("redbus", "redBus", "bus", "transact", _HANDOFF, "https://www.redbus.in/",
                 ("affiliate program; no public API found",), handoff=_redbus,
                 notes="redBus has no public booking API."),
    # ------------------------------------------------------------------ shopping (offers engine compares these; TEST)
    ProviderSpec("amazon", "Amazon", "shopping", "transact",
                 {**_HANDOFF, **_ops(search=S.PARTNER_ACCESS_REQUIRED, get_quote=S.PARTNER_ACCESS_REQUIRED)},
                 "https://www.amazon.in/", ("https://webservices.amazon.in/paapi5/documentation/",),
                 partner_program="Amazon Product Advertising API 5.0 (Amazon Associates account with qualifying sales)",
                 handoff=lambda p: f"https://www.amazon.in/s?k={quote(str(p.get('query') or ''))}",
                 notes="Amazon has no ordering API for outside apps; prices need PA-API partner access."),
    ProviderSpec("flipkart_shopping", "Flipkart", "shopping", "transact",
                 {**_HANDOFF, **_ops(search=S.PARTNER_ACCESS_REQUIRED, get_quote=S.PARTNER_ACCESS_REQUIRED)},
                 "https://www.flipkart.com/", ("https://affiliate.flipkart.com/",),
                 partner_program="Flipkart Affiliate API (approved affiliates)",
                 handoff=lambda p: f"https://www.flipkart.com/search?q={quote(str(p.get('query') or ''))}",
                 notes="Flipkart has no ordering API for outside apps."),
    ProviderSpec("croma", "Croma", "shopping", "transact", _HANDOFF, "https://www.croma.com/",
                 ("no public API found",), notes="Croma has no public API."),
    ProviderSpec("reliance_digital", "Reliance Digital", "shopping", "transact", _HANDOFF,
                 "https://www.reliancedigital.in/", ("no public API found",), notes="No public API."),
    ProviderSpec("myntra", "Myntra", "shopping", "transact", _HANDOFF, "https://www.myntra.com/",
                 ("affiliate program only",), notes="Myntra has no public ordering API."),
    # ------------------------------------------------------------------ rides, events, dining
    ProviderSpec("ola", "Ola", "ride", "transact", _HANDOFF, "https://www.olacabs.com/",
                 ("Ola's partner API is closed to new integrators",), notes="Ola has no open booking API."),
    ProviderSpec("bookmyshow", "BookMyShow", "events", "transact", _HANDOFF, "https://in.bookmyshow.com/",
                 ("no public API; partner integrations only",),
                 handoff=lambda p: f"https://in.bookmyshow.com/explore/events-{_slug(str(p.get('city') or 'pune'))}",
                 notes="BookMyShow has no public booking API."),
    ProviderSpec("district", "District (by Zomato)", "events", "transact", _HANDOFF, "https://www.district.in/",
                 ("no public API found",), notes="District has no public booking API."),
    ProviderSpec("swiggy_dineout", "Swiggy Dineout", "dining", "transact", _SWIGGY_PARTNER,
                 "https://www.swiggy.com/dineout", ("Swiggy partner program",),
                 partner_program="Swiggy partner program", notes="Table booking via Swiggy needs partner access."),
    # ------------------------------------------------------------------ web search and Google
    ProviderSpec("tavily", "Tavily web search", "web", "info",
                 _ops(search=S.CREDENTIALS_REQUIRED), "https://tavily.com/", ("https://docs.tavily.com/",),
                 credentials=("TAVILY_API_KEY",), adapter_built=True,
                 notes="Free plan: 1,000 credits/month. Without a key Apna uses Google News RSS + Wikipedia."),
    ProviderSpec("google_news", "Google News (RSS)", "web", "info", _ops(search=S.LIVE),
                 "https://news.google.com/", ("verified 2026-11-16: /rss/search?q=...&hl=en-IN&gl=IN&ceid=IN:en",),
                 adapter_built=True, notes="Headlines with links; Google's terms apply."),
    ProviderSpec("wikipedia", "Wikipedia", "web", "info", _ops(search=S.LIVE, get_details=S.LIVE),
                 "https://en.wikipedia.org/", ("https://en.wikipedia.org/api/rest_v1/",), adapter_built=True),
    ProviderSpec("google", "Google (Gmail + Calendar)", "email", "info",
                 _ops(search=S.CREDENTIALS_REQUIRED, get_details=S.CREDENTIALS_REQUIRED,
                      create_booking=S.CREDENTIALS_REQUIRED),
                 "https://myaccount.google.com/", ("https://developers.google.com/identity/protocols/oauth2/native-app",
                                                    "https://developers.google.com/gmail/api",
                                                    "https://developers.google.com/calendar/api"),
                 credentials=("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "PUBLIC_BASE_URL"), adapter_built=True,
                 notes="Each user connects their own account by OAuth. Gmail scopes need Google's verification "
                       "before public launch (test users only until then)."),
    # ------------------------------------------------------------------ weather
    ProviderSpec("open_meteo", "Open-Meteo", "weather", "info", _ops(search=S.LIVE, get_details=S.LIVE),
                 "https://open-meteo.com/", ("https://open-meteo.com/en/terms",),
                 notes="Free for non-commercial use; commercial use needs an Open-Meteo subscription."),
    # ------------------------------------------------------------------ Apna's TEST providers
    ProviderSpec("sandbox_grocery", "Apna sandbox grocery (TEST)", "grocery", "transact", _SANDBOX_COMMERCE,
                 "", ("apna.providers.grocery",), adapter_built=True,
                 notes="Simulated stores modelled on Blinkit, Instamart, Zepto, Flipkart Minutes, BB Now."),
    ProviderSpec("sandbox_offers", "Apna offers engine (TEST)", "shopping", "transact",
                 _ops(search=S.SANDBOX, get_quote=S.SANDBOX, create_booking=S.SANDBOX, initiate_checkout=S.SANDBOX,
                      get_status=S.SANDBOX, cancel=S.SANDBOX), "", ("apna.providers.offers",), adapter_built=True,
                 notes="Same item priced at every source with fees and taxes; products, food, flights, hotels, "
                       "buses, events, tables, ride fares."),
    ProviderSpec("sandbox_cinema", "Apna sandbox cinema (TEST)", "movies", "transact",
                 _ops(search=S.SANDBOX, get_quote=S.SANDBOX, create_booking=S.SANDBOX, initiate_checkout=S.SANDBOX,
                      get_status=S.SANDBOX), "", ("apna.providers.cinema",), adapter_built=True),
]
REGISTRY: dict[str, ProviderSpec] = {p.id: p for p in _SPECS}

_ALIASES = {
    "blinkit": "blinkit", "grofers": "blinkit", "zepto": "zepto", "instamart": "instamart",
    "swiggy instamart": "instamart", "bigbasket": "bigbasket", "big basket": "bigbasket", "bb now": "bigbasket",
    "bbnow": "bigbasket", "flipkart": "flipkart_minutes", "flipkart minutes": "flipkart_minutes",
    "swiggy": "swiggy_food", "zomato": "zomato", "uber": "uber", "rapido": "rapido",
    "urban company": "urban_company", "urbanclap": "urban_company", "urban clap": "urban_company",
    "skyscanner": "skyscanner", "booking": "booking_com", "booking.com": "booking_com", "agoda": "agoda",
    "trip.com": "tripcom", "tripcom": "tripcom", "tripadvisor": "tripadvisor", "makemytrip": "makemytrip",
    "mmt": "makemytrip", "redbus": "redbus", "red bus": "redbus", "razorpay": "razorpay", "phonepe": "phonepe_pg",
    "google pay": "google_pay", "gpay": "google_pay", "amazon": "amazon", "croma": "croma",
    "reliance digital": "reliance_digital", "myntra": "myntra", "ola": "ola", "bookmyshow": "bookmyshow",
    "bms": "bookmyshow", "district": "district", "dineout": "swiggy_dineout", "swiggy dineout": "swiggy_dineout",
}


def provider_named(text: str | None) -> ProviderSpec | None:
    t = (text or "").strip().lower()
    if t in REGISTRY:
        return REGISTRY[t]
    if t in _ALIASES:
        return REGISTRY[_ALIASES[t]]
    for alias, pid in sorted(_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return REGISTRY[pid]
    return None


def providers_for(domain: str) -> list[ProviderSpec]:
    return [p for p in _SPECS if p.domain == domain and not p.id.startswith("sandbox_")]


def credentials_present(spec: ProviderSpec, env: Mapping[str, str | None]) -> bool:
    return bool(spec.credentials) and all(env.get(k) for k in spec.credentials)


def status(provider_id: str, op: Op | str, env: Mapping[str, str | None] | None = None) -> S:
    spec = REGISTRY.get(provider_id)
    if spec is None:
        return S.UNSUPPORTED
    base = spec.ops.get(Op(op), S.UNSUPPORTED)
    if base == S.CREDENTIALS_REQUIRED and spec.adapter_built and credentials_present(spec, env or {}):
        return S.LIVE
    return base


_VERB = {Op.CREATE_CART: "ordering", Op.INITIATE_CHECKOUT: "checkout", Op.CREATE_BOOKING: "booking",
         Op.SEARCH: "search", Op.GET_QUOTE: "price quotes", Op.GET_STATUS: "tracking", Op.CANCEL: "cancelling",
         Op.UPDATE_CART: "cart changes", Op.GET_DETAILS: "details"}


def explain(spec: ProviderSpec, op: Op | str, st: S) -> str:
    """Plain-language reason, without the provider-name prefix (callers show the name)."""
    what = _VERB.get(Op(op), Op(op).value.replace("_", " "))
    if st == S.PARTNER_ACCESS_REQUIRED:
        program = (spec.partner_program or "a partner agreement").split(" (")[0]
        return f"{what} through Apna needs partner approval ({program}); not available yet."
    if st == S.CREDENTIALS_REQUIRED:
        need = ", ".join(spec.credentials) or "credentials"
        built = "" if spec.adapter_built else " (adapter not built yet)"
        return f"{what} needs {need} to be configured{built}."
    if st == S.HANDOFF_ONLY:
        return f"I can only open the {spec.name} app or site for you."
    return spec.notes or f"{what} isn't available."


def require(provider_id: str, op: Op | str, env: Mapping[str, str | None] | None = None) -> S:
    """Raise CapabilityUnavailable unless the operation can run (live or sandbox)."""
    st = status(provider_id, op, env)
    if st.usable:
        return st
    spec = REGISTRY.get(provider_id)
    raise CapabilityUnavailable(f"{spec.name}: {explain(spec, op, st)} {spec.notes}".strip() if spec
                                else f"Unknown provider {provider_id!r}.",
                                details={"provider": provider_id, "operation": str(op), "status": st.value,
                                         "handoff": handoff_link(provider_id, {})})


def handoff_link(provider_id: str, params: dict[str, Any]) -> str | None:
    spec = REGISTRY.get(provider_id)
    if spec is None or spec.ops.get(Op.HANDOFF) != S.HANDOFF_ONLY:
        return None
    return spec.handoff(params) if spec.handoff else spec.homepage


def env_snapshot() -> dict[str, str | None]:
    """Credential presence for status(); values are only checked for presence, never logged."""
    import os

    names = {c for p in _SPECS for c in p.credentials}
    return {n: os.environ.get(n) for n in names}
