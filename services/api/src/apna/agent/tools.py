"""Tool registry: static declarations of every tool the agent may use.

Risk level, integration and verification rule are part of the *declaration*; the planner
cannot change them. Argument models validate every call (model output is untrusted).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from apna.domain.enums import RiskLevel

# Sites the isolated browser may open (allow-listed sandbox sites). Must match browser.executor.SITES.
BROWSER_SITE_KEYS = frozenset({"sample_form", "news_test", "login_test"})
BROWSER_TOOLS = frozenset({"browser_read_page", "browser_submit_form"})

REF = re.compile(r"^\{\{(s\d{1,2})\.([a-z_]+)\}\}$")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AskUserArgs(_Args):
    question_key: str
    clause: str = Field(max_length=500)
    options: list[str] = Field(default_factory=list)
    partial: dict[str, Any] = Field(default_factory=dict)


class CreateReminderArgs(_Args):
    text: str = Field(min_length=1, max_length=300)
    fire_at_local: datetime
    timezone: str

    @field_validator("fire_at_local")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("fire_at_local must be timezone-aware")
        return v


class FindEmailArgs(_Args):
    query: str = Field(max_length=200)


class DraftEmailArgs(_Args):
    to_hint: str = Field(max_length=200)
    to_address: EmailStr | None = None
    subject: str = Field(max_length=300)
    body: str = Field(max_length=10000)
    reply_to_thread: str | None = None


class SendEmailArgs(_Args):
    draft_id: str
    # Snapshot of what will be sent; filled during step preparation from the draft itself.
    to_address: EmailStr | None = None
    subject: str | None = None
    body: str | None = None


class SearchTrainsArgs(_Args):
    origin: str = Field(max_length=80)
    destination: str = Field(max_length=80)
    travel_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    travel_class: str | None = None
    time_preference: str | None = None


class FindMovieArgs(_Args):
    movie: str = Field(min_length=1, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    when_text: str | None = Field(default=None, max_length=120)
    tickets: int | None = Field(default=None, ge=1, le=20)
    show_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")  # set by grounding


class BookMovieArgs(_Args):
    movie: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=80)
    theatre: str | None = Field(default=None, max_length=120)
    when_text: str | None = Field(default=None, max_length=120)
    show_time: str | None = Field(default=None, max_length=40)
    tickets: int = Field(ge=1, le=10)
    rows: list[str] = Field(default_factory=list, max_length=6)
    seat_class: str | None = Field(default=None, max_length=40)
    show_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")  # set by grounding


class PayMovieArgs(_Args):
    hold_id: str
    # Snapshot of the hold, filled during step preparation so the approval binds to the exact purchase.
    amount_minor: int | None = None
    currency: str | None = None
    merchant: str | None = None
    movie: str | None = None
    theatre: str | None = None
    starts_at: str | None = None
    seats: list[str] | None = None
    seat_class: str | None = None


class GroceryItem(_Args):
    name: str = Field(min_length=1, max_length=80)
    qty: int = Field(default=1, ge=1, le=20)
    size: str | None = Field(default=None, max_length=30)
    brand: str | None = Field(default=None, max_length=40)


class OrderGroceriesArgs(_Args):
    items: list[GroceryItem] = Field(min_length=1, max_length=25)
    store: str | None = Field(default=None, max_length=40)
    stores: list[str] | None = Field(default=None, max_length=5)  # "Blinkit ya Zepto": compare only these
    choose: Literal["cheapest", "fastest"] | None = None  # "jo sasta ho" -> cheapest
    city: str | None = Field(default=None, max_length=80)
    area: str | None = Field(default=None, max_length=120)


class CartChange(_Args):
    action: Literal["add", "remove", "set"]
    name: str = Field(min_length=1, max_length=80)
    qty: int | None = Field(default=None, ge=1, le=20)
    size: str | None = Field(default=None, max_length=30)
    brand: str | None = Field(default=None, max_length=60)


class UpdateCartArgs(_Args):
    changes: list[CartChange] = Field(min_length=1, max_length=10)


class NoArgs(_Args):
    pass


class WeatherArgs(_Args):
    place: str = Field(min_length=1, max_length=80)
    when_text: str | None = Field(default=None, max_length=60)
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")  # set by grounding


class HandoffArgs(_Args):
    domain: Literal["ride", "food", "home", "travel", "bus", "grocery"]
    provider: str | None = Field(default=None, max_length=40)
    summary: str = Field(default="", max_length=200)  # what the user wants, in a few words
    query: str | None = Field(default=None, max_length=80)
    pickup: str | None = Field(default=None, max_length=120)
    dropoff: str | None = Field(default=None, max_length=120)
    origin: str | None = Field(default=None, max_length=80)
    destination: str | None = Field(default=None, max_length=80)
    checkin: str | None = Field(default=None, max_length=10)
    checkout: str | None = Field(default=None, max_length=10)
    adults: int | None = Field(default=None, ge=1, le=20)
    rooms: int | None = Field(default=None, ge=1, le=10)


class PayGroceryArgs(_Args):
    order_id: str
    # Snapshot of the cart, filled during step preparation so the approval binds to the exact order.
    amount_minor: int | None = None
    currency: str | None = None
    merchant: str | None = None
    store: str | None = None
    items: list[str] | None = None
    deliver_to: str | None = None
    payment_method: Literal["razorpay", "sandbox"] | None = None


_DATE = r"^\d{4}-\d{2}-\d{2}$"
Vertical = Literal["product", "food", "flight", "hotel", "bus", "event", "table", "ride"]


class SearchOffersArgs(_Args):
    vertical: Vertical
    query: str = Field(default="", max_length=120)
    city: str | None = Field(default=None, max_length=80)
    area: str | None = Field(default=None, max_length=120)
    origin: str | None = Field(default=None, max_length=80)
    destination: str | None = Field(default=None, max_length=80)
    when_text: str | None = Field(default=None, max_length=60)
    checkin_text: str | None = Field(default=None, max_length=60)
    checkout_text: str | None = Field(default=None, max_length=60)
    qty: int | None = Field(default=None, ge=1, le=20)
    rooms: int | None = Field(default=None, ge=1, le=10)
    time_pref: str | None = Field(default=None, max_length=40)
    class_pref: str | None = Field(default=None, max_length=40)
    sources: list[str] | None = Field(default=None, max_length=6)
    budget_max: int | None = Field(default=None, ge=1, le=10_000_000)  # rupees
    sort: Literal["cheapest", "fastest", "earliest", "best_rated"] | None = None
    day: str | None = Field(default=None, pattern=_DATE)  # set by grounding
    checkin: str | None = Field(default=None, pattern=_DATE)  # set by grounding
    checkout: str | None = Field(default=None, pattern=_DATE)  # set by grounding


class WatchPriceArgs(SearchOffersArgs):
    target_price: int | None = Field(default=None, ge=1, le=10_000_000)  # rupees
    days: int | None = Field(default=None, ge=1, le=60)


class ManageWatchArgs(_Args):
    action: Literal["list", "stop"]
    name: str | None = Field(default=None, max_length=120)


class BookOfferArgs(_Args):
    offer_id: str = Field(pattern=r"^OFR-[0-9A-F]{8}$")


class PayOfferArgs(_Args):
    booking_id: str
    # Snapshot of the hold, filled during step preparation so the approval binds to the exact purchase.
    amount_minor: int | None = None
    currency: str | None = None
    merchant: str | None = None
    title: str | None = None
    when: str | None = None
    vertical: str | None = None
    payment_method: Literal["razorpay", "sandbox", "none"] | None = None


class CancelBookingArgs(_Args):
    ref: str | None = Field(default=None, max_length=40)


class ShoppingListArgs(_Args):
    action: Literal["add", "remove", "show", "clear"]
    items: list[GroceryItem] = Field(default_factory=list, max_length=30)


class OrderListArgs(_Args):
    store: str | None = Field(default=None, max_length=40)
    stores: list[str] | None = Field(default=None, max_length=5)
    choose: Literal["cheapest", "fastest"] | None = None
    city: str | None = Field(default=None, max_length=80)
    area: str | None = Field(default=None, max_length=120)


class WebSearchArgs(_Args):
    query: str = Field(min_length=2, max_length=200)
    kind: Literal["news", "ipo", "general"] | None = None


class ReadInboxArgs(_Args):
    query: str = Field(default="", max_length=200)


class CalendarListArgs(_Args):
    when_text: str | None = Field(default=None, max_length=60)
    days: int = Field(default=1, ge=1, le=7)
    day: str | None = Field(default=None, pattern=_DATE)  # set by grounding


class CalendarCreateArgs(_Args):
    title: str = Field(min_length=1, max_length=200)
    start_local: datetime
    timezone: str
    duration_min: int = Field(default=60, ge=5, le=24 * 60)

    @field_validator("start_local")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start_local must be timezone-aware")
        return v


class AnswerQuestionArgs(_Args):
    answer: str = Field(min_length=1, max_length=3500)


class BrowserSubmitFormArgs(_Args):
    site_key: str
    fields: dict[str, str]


class BrowserReadPageArgs(_Args):
    site_key: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[_Args]
    risk: RiskLevel
    integration: str
    task_queue: str = "default"  # "default" | "browser"
    planner_visible: bool = True
    planner_args_hint: str = ""

    @property
    def requires_approval(self) -> bool:
        return self.risk >= RiskLevel.EXTERNAL_COMMUNICATION


TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        ToolSpec("ask_user", "Ask the user a clarifying question.", AskUserArgs, RiskLevel.READ_ONLY, "core",
                 planner_args_hint='{"question_key": str, "clause": str}'),
        ToolSpec("create_reminder", "Schedule a reminder notification for the user.", CreateReminderArgs,
                 RiskLevel.INTERNAL_REVERSIBLE, "scheduler",
                 planner_args_hint='{"text": str, "when_text": "the user\'s own words for when, verbatim"}'),
        ToolSpec("find_email", "Search the user's connected mailbox for a thread (read-only).", FindEmailArgs,
                 RiskLevel.READ_ONLY, "email", planner_args_hint='{"query": str}'),
        ToolSpec("draft_email", "Create an email draft. Never sends.", DraftEmailArgs,
                 RiskLevel.INTERNAL_REVERSIBLE, "email",
                 planner_args_hint='{"to_hint": str, "subject": str, "body": str, '
                                   '"reply_to_thread": "{{sN.thread_ref}} or null"}'),
        ToolSpec("send_email", "Send a previously created draft. Requires user approval.", SendEmailArgs,
                 RiskLevel.EXTERNAL_COMMUNICATION, "email",
                 planner_args_hint='{"draft_id": "{{sN.draft_id}}"}'),
        ToolSpec("search_trains", "Search Indian Railways trains between two places on a date.",
                 SearchTrainsArgs, RiskLevel.READ_ONLY, "travel.rail",
                 planner_args_hint='{"origin": str|null, "destination": str, "date_text": "verbatim words", '
                                   '"travel_class": str|null, "time_preference": str|null}'),
        ToolSpec("find_movie_tickets", "Movie tickets: give the official BookMyShow page for the city (the "
                 "user books and pays there). Never shows invented showtimes or prices.", FindMovieArgs,
                 RiskLevel.READ_ONLY, "movies",
                 planner_args_hint='{"movie": "title in English letters", "city": str|null, "when_text": "verbatim words"|null, '
                                   '"tickets": int|null}'),
        ToolSpec("book_movie_tickets", "TEST sandbox cinema: find the show, check seats in the rows the user "
                 "wants and hold them (or offer other rows, shows or theatres). Pair with pay_movie_booking.",
                 BookMovieArgs, RiskLevel.INTERNAL_REVERSIBLE, "movies.sandbox",
                 planner_args_hint='{"movie": "title in English letters", "city": str, "theatre": str|null, '
                                   '"when_text": "verbatim day words"|null, "show_time": "verbatim, e.g. 10:45 pm"|null, '
                                   '"tickets": int, "rows": ["A"]|[], "seat_class": str|null}'),
        ToolSpec("pay_movie_booking", "TEST payment for a held movie booking. Requires the user's approval.",
                 PayMovieArgs, RiskLevel.FINANCIAL_CONSEQUENTIAL, "payments.sandbox",
                 planner_args_hint='{"hold_id": "{{sN.hold_id}}"}'),
        ToolSpec("order_groceries", "Groceries (TEST sandbox modelled on Blinkit/Instamart/Zepto/Flipkart Minutes/"
                 "BB Now; the real apps have no ordering API): compare equivalent quantities incl. fees, ask about "
                 "product variants, build a cart. Pair with pay_grocery_order.", OrderGroceriesArgs,
                 RiskLevel.INTERNAL_REVERSIBLE, "grocery.sandbox",
                 planner_args_hint='{"items": [{"name": str, "qty": "number of packs (1 if size given)", '
                                   '"size": "total amount e.g. 2 litre / 1 kg / 12 for 12 eggs"|null, "brand": str|null}], '
                                   '"store": str|null, "stores": ["Blinkit","Zepto"]|null, '
                                   '"choose": "cheapest"|"fastest"|null, "city": str|null, "area": str|null}'),
        ToolSpec("update_grocery_cart", "Change the user's latest open grocery cart (add / remove / set items). "
                 "Pair with pay_grocery_order.", UpdateCartArgs, RiskLevel.INTERNAL_REVERSIBLE, "grocery.sandbox",
                 planner_args_hint='{"changes": [{"action": "add|remove|set", "name": str, "qty": int|null, '
                                   '"size": str|null, "brand": str|null}]}'),
        ToolSpec("cancel_grocery_order", "Cancel the user's latest unpaid grocery cart.", NoArgs,
                 RiskLevel.INTERNAL_REVERSIBLE, "grocery.sandbox", planner_args_hint="{}"),
        ToolSpec("pay_grocery_order", "Payment for a grocery cart (Razorpay link if configured, else TEST). "
                 "Requires the user's approval.", PayGroceryArgs, RiskLevel.FINANCIAL_CONSEQUENTIAL, "payments",
                 planner_args_hint='{"order_id": "{{sN.order_id}}"}'),
        ToolSpec("get_order_status", "Status of the user's recent orders, bookings and payments ('what happened "
                 "to my order?').", NoArgs, RiskLevel.READ_ONLY, "core", planner_args_hint="{}"),
        ToolSpec("get_weather", "Live weather and forecast for a place (Open-Meteo).", WeatherArgs,
                 RiskLevel.READ_ONLY, "weather",
                 planner_args_hint='{"place": str, "when_text": "verbatim day words"|null}'),
        ToolSpec("provider_handoff", "Home services (Urban Company), or when the user only wants to OPEN a named "
                 "app (Uber, Swiggy, Booking.com, Skyscanner, redBus...): explain what Apna can do there and open "
                 "the app with details filled. For prices/booking use search_offers instead.",
                 HandoffArgs, RiskLevel.READ_ONLY, "handoff",
                 planner_args_hint='{"domain": "ride|food|home|travel|bus", "provider": str|null, "summary": str, '
                                   '"query": str|null, "pickup": str|null, "dropoff": str|null, "origin": str|null, '
                                   '"destination": str|null, "checkin": "YYYY-MM-DD"|null, "checkout": "YYYY-MM-DD"|null, '
                                   '"adults": int|null, "rooms": int|null}'),
        ToolSpec("search_offers", "Compare the SAME product or service across sellers/apps and rank by the full "
                 "price (fees, taxes, discounts): products (Amazon, Flipkart, Croma, Reliance Digital, Myntra), "
                 "restaurant food (Swiggy vs Zomato), flights, hotels, buses, events/shows, restaurant tables "
                 "(best discount), cab/auto/bike fares (Uber, Ola, Rapido). TEST prices; the user picks one to book.",
                 SearchOffersArgs, RiskLevel.READ_ONLY, "offers.sandbox",
                 planner_args_hint='{"vertical": "product|food|flight|hotel|bus|event|table|ride", "query": str, '
                                   '"city": str|null, "area": str|null, "origin": str|null, "destination": str|null, '
                                   '"when_text": "verbatim day words"|null, "checkin_text": str|null, '
                                   '"checkout_text": str|null, "qty": "units / tickets / travellers / guests"|null, '
                                   '"rooms": int|null, "time_pref": "morning|afternoon|evening|night|8 pm"|null, '
                                   '"class_pref": str|null, "sources": ["Amazon"]|null, "budget_max": rupees|null, '
                                   '"sort": "cheapest|fastest|earliest|best_rated"|null}'),
        ToolSpec("book_offer", "Hold one offer from a comparison the user chose (TEST). Pair with pay_offer.",
                 BookOfferArgs, RiskLevel.INTERNAL_REVERSIBLE, "offers.sandbox",
                 planner_args_hint='{"offer_id": "OFR-XXXXXXXX"}'),
        ToolSpec("pay_offer", "Payment / confirmation for a held offer (Razorpay link if configured, else TEST). "
                 "Requires the user's approval.", PayOfferArgs, RiskLevel.FINANCIAL_CONSEQUENTIAL, "payments",
                 planner_args_hint='{"booking_id": "{{sN.booking_id}}"}'),
        ToolSpec("cancel_booking", "Cancel the user's latest booking from a comparison (flight, hotel, bus, event, "
                 "table, product, food) and show the refund under its policy.", CancelBookingArgs,
                 RiskLevel.INTERNAL_REVERSIBLE, "offers.sandbox", planner_args_hint='{"ref": "booking code"|null}'),
        ToolSpec("watch_price", "Price-drop alert: watch a product/flight/hotel/... and message the user when it gets "
                 "cheaper (or below target_price).", WatchPriceArgs, RiskLevel.INTERNAL_REVERSIBLE, "offers.sandbox",
                 planner_args_hint='{same args as search_offers, "target_price": rupees|null, "days": int|null}'),
        ToolSpec("manage_price_watch", "List or stop the user's price-drop alerts.", ManageWatchArgs,
                 RiskLevel.INTERNAL_REVERSIBLE, "offers.sandbox",
                 planner_args_hint='{"action": "list|stop", "name": str|null}'),
        ToolSpec("shopping_list", "The user's shopping list: add / remove / show (shareable text) / clear items.",
                 ShoppingListArgs, RiskLevel.INTERNAL_REVERSIBLE, "core",
                 planner_args_hint='{"action": "add|remove|show|clear", "items": [{"name": str, "qty": int, '
                                   '"size": str|null, "brand": str|null}]}'),
        ToolSpec("order_shopping_list", "Order everything on the shopping list as groceries (compare stores, cart). "
                 "Pair with pay_grocery_order.", OrderListArgs, RiskLevel.INTERNAL_REVERSIBLE, "grocery.sandbox",
                 planner_args_hint='{"store": str|null, "stores": [str]|null, "choose": "cheapest"|"fastest"|null, '
                                   '"city": str|null, "area": str|null}'),
        ToolSpec("web_search", "Search the live web: news, IPOs, prices/rates, scores, current events, anything "
                 "recent or factual you are unsure about. Returns sources with links.", WebSearchArgs,
                 RiskLevel.READ_ONLY, "web", planner_args_hint='{"query": str, "kind": "news|ipo|general"|null}'),
        ToolSpec("read_inbox", "Show the user's latest emails (connected Gmail, or the sandbox mailbox).",
                 ReadInboxArgs, RiskLevel.READ_ONLY, "email", planner_args_hint='{"query": str}'),
        ToolSpec("connect_google", "Give the user a secure Google sign-in link to connect Gmail and Calendar.",
                 NoArgs, RiskLevel.READ_ONLY, "google", planner_args_hint="{}"),
        ToolSpec("disconnect_google", "Disconnect Google and delete Apna's stored Google access.", NoArgs,
                 RiskLevel.INTERNAL_REVERSIBLE, "google", planner_args_hint="{}"),
        ToolSpec("calendar_list", "Show events from the user's Google Calendar.", CalendarListArgs,
                 RiskLevel.READ_ONLY, "google.calendar",
                 planner_args_hint='{"when_text": "verbatim day words"|null, "days": int}'),
        ToolSpec("calendar_create_event", "Add an event to the user's Google Calendar (no invitees). Requires "
                 "approval.", CalendarCreateArgs, RiskLevel.EXTERNAL_COMMUNICATION, "google.calendar",
                 planner_args_hint='{"title": str, "when_text": "verbatim date and time words", '
                                   '"duration_min": int|null}'),
        ToolSpec("answer_question", "Answer a general question directly (recipes, facts, explanations, "
                 "advice). Write the answer in the user's language.", AnswerQuestionArgs,
                 RiskLevel.READ_ONLY, "core", planner_args_hint='{"answer": "the full answer text"}'),
        ToolSpec("browser_read_page", "Read an allow-listed web page in the user's isolated browser.",
                 BrowserReadPageArgs, RiskLevel.READ_ONLY, "browser", task_queue="browser",
                 planner_args_hint='{"site_key": str}'),
        ToolSpec("browser_submit_form", "Fill and submit a form on an allow-listed site. Requires approval.",
                 BrowserSubmitFormArgs, RiskLevel.EXTERNAL_COMMUNICATION, "browser", task_queue="browser",
                 planner_args_hint='{"site_key": str, "fields": {name: value}}'),
    ]
}


def get_tool(name: str) -> ToolSpec:
    from apna.core.errors import PolicyViolation

    spec = TOOLS.get(name)
    if spec is None:
        raise PolicyViolation(f"Unknown tool: {name!r}")
    return spec


def has_refs(args: dict[str, Any]) -> bool:
    return any(isinstance(v, str) and REF.match(v) for v in args.values())


def substitute_refs(args: dict[str, Any], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Replace "{{s1.field}}" placeholders with outputs of completed dependency steps."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and (m := REF.match(v)):
            step, field = m.groups()
            if step not in results or field not in results[step]:
                raise KeyError(f"Unresolved reference {v}")
            out[k] = results[step][field]
        else:
            out[k] = v
    return out
