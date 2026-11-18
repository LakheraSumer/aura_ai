"""Executors: price comparison + TEST bookings (offers engine), price-drop watches, shopping lists,
web search, inbox, Google connection and Calendar.

Booking flow: search_offers shows the same item at every source, cheapest total first, with the savings
→ the user picks a number → "book offer OFR-…" is planned deterministically (direct_plan) → book_offer
re-quotes and holds it → pay_offer takes an exact-amount approval → TEST confirmation (or a Razorpay
link when keys are configured). Every sandbox result says TEST and links the real seller.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from apna.agent import policy
from apna.domain.enums import StepStatus
from apna.providers import offers

if TYPE_CHECKING:
    from apna.agent.executors import ExecResult, ToolContext

IST = ZoneInfo("Asia/Kolkata")
OFFER_QUESTIONS = frozenset({"offer_choice", "product_choice", "offer_details_needed"})
OFFER_SET_TTL = timedelta(minutes=30)
LABEL = {"product": "product", "food": "food order", "flight": "flight", "hotel": "hotel", "bus": "bus",
         "event": "event", "table": "table", "ride": "ride"}

_SOURCE_ALIASES = {
    "amazon": "amazon", "flipkart": "flipkart", "croma": "croma", "reliance digital": "reliance_digital",
    "reliance": "reliance_digital", "myntra": "myntra", "swiggy": "swiggy_food", "zomato": "zomato",
    "makemytrip": "makemytrip", "make my trip": "makemytrip", "mmt": "makemytrip", "trip.com": "tripcom",
    "tripcom": "tripcom", "booking.com": "booking_com", "booking": "booking_com", "agoda": "agoda",
    "airline": "airline", "indigo": "airline", "air india": "airline", "akasa": "airline", "spicejet": "airline",
    "redbus": "redbus", "red bus": "redbus", "bookmyshow": "bookmyshow", "bms": "bookmyshow", "district": "district",
    "dineout": "swiggy_dineout", "swiggy dineout": "swiggy_dineout", "uber": "uber", "ola": "ola", "rapido": "rapido",
}


def _result(status: StepStatus, data: dict[str, Any] | None = None, error: str | None = None) -> ExecResult:
    from apna.agent.executors import ExecResult

    return ExecResult(status, data or {}, error)


def source_ids(names: list[str] | None) -> list[str]:
    out: list[str] = []
    for n in names or []:
        t = n.strip().lower()
        sid = _SOURCE_ALIASES.get(t) or next((v for k, v in sorted(_SOURCE_ALIASES.items(), key=lambda kv: -len(kv[0]))
                                              if re.search(rf"\b{re.escape(k)}\b", t)), None)
        if sid and sid not in out:
            out.append(sid)
    return out


def offer_query(a: Any) -> offers.OfferQuery:
    d = lambda s: date.fromisoformat(s) if s else None  # noqa: E731
    vertical = a.vertical
    qty = a.qty or 1
    if vertical == "hotel" and not a.qty:
        qty = 2  # adults
    return offers.OfferQuery(
        vertical=vertical, query=a.query or "", city=a.city, area=a.area, origin=a.origin, destination=a.destination,
        day=d(a.day), checkin=d(a.checkin) or d(a.day), checkout=d(a.checkout), qty=qty, rooms=a.rooms or 1,
        time_pref=a.time_pref, class_pref=a.class_pref, sources=source_ids(a.sources),
        budget_max_minor=a.budget_max * 100 if a.budget_max else None, sort=a.sort or "cheapest")


def offer_request(args: dict[str, Any], tool: str = "search_offers") -> str:
    """The request in words, so an answer can be merged and re-planned."""
    v = args.get("vertical", "")
    verb = "alert me when cheaper:" if tool == "watch_price" else "compare prices for"
    bits = [verb, LABEL.get(v, v), args.get("query") or ""]
    if args.get("origin") or args.get("destination"):
        bits.append(f"from {args.get('origin') or '?'} to {args.get('destination') or '?'}")
    if args.get("city"):
        bits.append(f"in {args['city']}")
    if args.get("area"):
        bits.append(f"near {args['area']}")
    for k in ("when_text", "checkin_text"):
        if args.get(k):
            bits.append(str(args[k]))
    if args.get("checkout_text"):
        bits.append(f"to {args['checkout_text']}")
    if args.get("qty"):
        bits.append(f"for {args['qty']}")
    for k in ("time_pref", "class_pref"):
        if args.get(k):
            bits.append(str(args[k]))
    if args.get("sources"):
        bits.append("on " + " or ".join(args["sources"]))
    if args.get("budget_max"):
        bits.append(f"under ₹{args['budget_max']}")
    if args.get("target_price"):
        bits.append(f"target ₹{args['target_price']}")
    return re.sub(r"\s{2,}", " ", " ".join(str(b) for b in bits if b)).strip()


# ------------------------------------------------------------------ comparison


def _group(found: list[offers.Offer]) -> list[tuple[offers.Offer, list[offers.Offer]]]:
    """[(best offer for an item, the same item at other sources)], in ranked order."""
    order: list[str] = []
    by: dict[str, list[offers.Offer]] = {}
    for o in found:
        if o.item_key not in by:
            order.append(o.item_key)
            by[o.item_key] = []
        by[o.item_key].append(o)
    return [(by[k][0], by[k][1:]) for k in order]


def option_text(o: offers.Offer, others: list[offers.Offer]) -> str:
    price = (f"{o.details.get('discount_pct')}% off" if o.vertical == "table" else offers.rupees(o.total))
    extra = []
    if o.vertical in ("flight", "bus", "hotel", "event"):
        extra.append(o.subtitle)
    elif o.vertical == "food":
        extra.append(o.eta)
    elif o.vertical == "ride":
        extra.append(o.eta)
    elif o.vertical == "product":
        extra.append(o.eta)
    if o.conditions.get("cancellation") and o.vertical in ("flight", "hotel"):
        extra.append(o.conditions["cancellation"])
    if o.vertical == "table":
        extra.append(datetime.fromisoformat(o.when).strftime("%I:%M %p").lstrip("0") if o.when else "")
    also = ""
    if others:
        def p(x: offers.Offer) -> str:
            return f"{x.details.get('discount_pct')}% off" if x.vertical == "table" else offers.rupees(x.total)
        also = " (also " + ", ".join(f"{x.source_name} {p(x)}" for x in others[:3]) + ")"
    title = o.title if o.vertical != "product" else o.source_name
    via = "" if o.vertical == "product" else f" · {o.source_name}"
    return f"{price} · {title}{via} · {' · '.join(e for e in extra if e)}{also}".replace(" ·  · ", " · ")


async def _save_set(ctx: ToolContext, vertical: str, args: dict[str, Any], found: list[offers.Offer]) -> uuid.UUID:
    from apna.db.models import OfferSet

    row = OfferSet(user_id=ctx.user_id, vertical=vertical, query=args, offers=[o.to_dict() for o in found[:60]],
                   expires_at=datetime.now(UTC) + OFFER_SET_TTL)
    ctx.session.add(row)
    await ctx.session.flush()
    return row.id


def _links(found: list[offers.Offer]) -> list[dict[str, str]]:
    seen: dict[str, str] = {}
    for o in found:
        seen.setdefault(o.source_name, o.link)
    return [{"source": k, "link": v} for k, v in list(seen.items())[:6]]


async def exec_search_offers(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import SearchOffersArgs

    a = SearchOffersArgs.model_validate(args)
    q = offer_query(a)
    found, meta = offers.search(q)
    base = {"vertical": a.vertical, "request": offer_request(args), "args": args, "test": True,
            "tool": "search_offers"}
    if meta.get("ambiguous"):
        return _result(StepStatus.AWAITING_USER_ACTION, {**base, "question_key": "product_choice",
                                                         "clause": a.query, "choices": meta["ambiguous"]})
    if not found:
        why = {"flight": "I only know these airports: " + ", ".join(sorted({c.title() for c in offers._AIRPORTS})),
               "product": "I couldn't match that to a specific product in the sandbox catalogue."}.get(a.vertical, "")
        return _result(StepStatus.COMPLETED_LOCALLY, {**base, "kind": "no_offers", "why": why})
    groups = _group(found)
    single_item = len(groups) == 1
    shown = [(o, []) for o in found[:6]] if single_item else groups[:6]
    choices = [option_text(o, others) for o, others in shown]
    set_id = await _save_set(ctx, a.vertical, args, found)
    sv = offers.savings(found)
    data = {**base, "offer_set_id": str(set_id), "choices": choices, "offer_ids": [o.id for o, _ in shown],
            "durations": [o.duration_min for o, _ in shown], "sources": sv.get("sources_compared", []),
            "save_same_minor": sv.get("vs_same_item_elsewhere_minor", 0),
            "save_max_minor": sv.get("vs_most_expensive_minor", 0), "best_title": found[0].title,
            "best_source": found[0].source_name, "links": _links(found), "product": meta.get("product"),
            "conditions": found[0].conditions}
    if a.vertical == "ride":  # fare estimates only: rides are booked in the app (no TEST ride bookings)
        return _result(StepStatus.COMPLETED_LOCALLY, {**data, "kind": "estimates"})
    return _result(StepStatus.AWAITING_USER_ACTION, {**data, "question_key": "offer_choice", "clause": base["request"]})


_STRIP = re.compile(r"\b(book|karo|kar\s*do|kardo|please|pls|wala|vala|chahiye|le\s*lo|lelo|lo|option|the|one|i\s*want|"
                    r"want|choose|select|go\s*with|ok|okay|confirm)\b", re.I)
_REFINE = re.compile(r"morning|afternoon|evening|night|subah|shaam|raat|cheaper|under|below|budget|only|sirf|ac\b|"
                     r"sleeper|seater|gold|silver|platinum|non.?stop|refundable|breakfast|bike|auto|sedan|rating|"
                     r"fastest|earliest|kal|tomorrow|parso", re.I)


def resolve_offer_answer(key: str, r: dict[str, Any], answer: str) -> str | None:
    """Deterministic merge for comparison questions. None -> treat the answer as a new request."""
    from apna.agent.commerce import _pick

    choices, ids = list(r.get("choices") or []), list(r.get("offer_ids") or [])
    ans = answer.strip()
    low = ans.lower()
    if key == "product_choice":
        chosen = _pick(choices, _STRIP.sub(" ", low).strip() or low)
        if not chosen:
            return None
        return offer_request({**(r.get("args") or {}), "query": chosen}, r.get("tool") or "search_offers")
    if key == "offer_details_needed":
        return f"{r.get('request') or r.get('clause') or ''} {ans}".strip()
    if key != "offer_choice" or not ids:
        return None
    if re.search(r"\b(alert|notify|watch|track|batana|bata\s*dena)\b", low):
        return offer_request(r.get("args") or {}, "watch_price")
    idx: int | None = None
    if re.search(r"cheap|sasta|sasti|lowest|best|sabse\s*kam|^1$", low):
        idx = 0
    elif re.search(r"fast|jaldi|quick", low) and any(r.get("durations") or []):
        durs = [d if d is not None else 10**6 for d in r.get("durations") or []]
        idx = durs.index(min(durs))
    else:
        cleaned = _STRIP.sub(" ", low)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        chosen = _pick(choices, cleaned) if cleaned else None
        if chosen is None and cleaned:
            hit = [c for c in choices if any(w in c.lower() for w in cleaned.split() if len(w) > 3)]
            chosen = hit[0] if hit else None  # choices are cheapest-first: first match is the cheapest there
        if chosen is not None:
            idx = choices.index(chosen)
    if idx is not None and idx < len(ids):
        return f"book offer {ids[idx]}"
    if _REFINE.search(low) and len(low.split()) <= 10:
        return f"{r.get('request')}, {ans}"  # refinement: re-plan the same search with the extra condition
    return None


# ------------------------------------------------------------------ booking


async def _find_offer(ctx: ToolContext, offer_id: str) -> tuple[Any, dict[str, Any]] | None:
    from apna.db.models import OfferSet

    rows = (await ctx.session.execute(select(OfferSet).where(OfferSet.user_id == ctx.user_id)
                                      .order_by(OfferSet.created_at.desc()).limit(15))).scalars().all()
    for row in rows:
        for o in row.offers:
            if o.get("id") == offer_id:
                return row, o
    return None


async def exec_book_offer(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import BookOfferArgs, SearchOffersArgs
    from apna.db.models import SandboxBooking

    a = BookOfferArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    existing = (await ctx.session.execute(select(SandboxBooking).where(
        SandboxBooking.idempotency_key == ctx.idempotency_key))).scalar_one_or_none()
    if existing is not None:
        return _held(existing, None)
    hit = await _find_offer(ctx, a.offer_id)
    if hit is None:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "offer_gone", "test": True})
    row, stored = hit
    # Always re-quote: never hold at a price from an old list.
    sargs = SearchOffersArgs.model_validate(row.query)
    fresh, _meta = offers.search(offer_query(sargs))
    cur = next((o for o in fresh if o.id == a.offer_id), None)
    if cur is None:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "offer_gone", "test": True,
                                                      "request": offer_request(row.query),
                                                      "title": stored.get("title")})
    old_total = int(stored["breakdown"]["total_minor"])
    b = SandboxBooking(user_id=ctx.user_id, idempotency_key=ctx.idempotency_key, vertical=cur.vertical,
                       source=cur.source, offer_id=cur.id, offer=cur.to_dict(), amount_minor=cur.total, status="HELD",
                       expires_at=datetime.now(UTC) + timedelta(minutes=ctx.settings.offer_hold_minutes))
    ctx.session.add(b)
    await ctx.session.flush()
    return _held(b, old_total if old_total != cur.total else None)


def _held(b: Any, old_total: int | None) -> ExecResult:
    o = b.offer
    return _result(StepStatus.COMPLETED_LOCALLY, {
        "kind": "held", "booking_id": str(b.id), "vertical": b.vertical, "title": o["title"], "subtitle": o["subtitle"],
        "source_name": o["source_name"], "when": o.get("when"), "breakdown": o["breakdown"], "qty": o["qty"],
        "unit_minor": o["unit_minor"], "amount_minor": b.amount_minor, "conditions": o.get("conditions") or {},
        "hold_until": b.expires_at.isoformat(), "link": o.get("link"), "eta": o.get("eta"),
        "old_total_minor": old_total, "test": True})


def _merchant(b: Any) -> str:
    return f"{b.offer['source_name']} (TEST sandbox)"


async def prepare_offer_payment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from apna.agent.tools import PayOfferArgs
    from apna.db.models import SandboxBooking

    a = PayOfferArgs.model_validate(args)
    b = await ctx.session.get(SandboxBooking, uuid.UUID(a.booking_id))
    if b is None or b.user_id != ctx.user_id:
        raise ValueError("Booking hold not found.")
    method = "none" if b.amount_minor == 0 else ("razorpay" if ctx.settings.razorpay_configured() else "sandbox")
    return {"booking_id": a.booking_id, "amount_minor": b.amount_minor, "currency": "INR", "merchant": _merchant(b),
            "title": b.offer["title"], "when": b.offer.get("when"), "vertical": b.vertical, "payment_method": method}


def _code(vertical: str) -> str:
    return f"TEST-{vertical[:3].upper()}-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))


async def confirm_booking(session: Any, booking_id: uuid.UUID, user_id: uuid.UUID, payment_ref: str) -> Any:
    """HELD / PAYMENT_PENDING -> CONFIRMED exactly once (row lock); returns the booking."""
    from apna.db.models import SandboxBooking

    b = (await session.execute(select(SandboxBooking).where(SandboxBooking.id == booking_id,
                                                           SandboxBooking.user_id == user_id)
                               .with_for_update())).scalar_one()
    if b.status == "CONFIRMED":
        return b
    if b.status not in ("HELD", "PAYMENT_PENDING"):
        raise ValueError(f"Booking is {b.status.lower()}.")
    b.status, b.booking_code, b.payment_ref = "CONFIRMED", _code(b.vertical), payment_ref[:60]
    b.confirmed_at = datetime.now(UTC)
    await session.flush()
    return b


async def exec_pay_offer(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import PayOfferArgs
    from apna.db.models import SandboxBooking
    from apna.providers import payments

    a = PayOfferArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    b = await ctx.session.get(SandboxBooking, uuid.UUID(a.booking_id))
    if b is None or b.user_id != ctx.user_id:
        return _result(StepStatus.FAILED, error="Booking hold not found.")
    if (b.amount_minor, b.offer["title"]) != (a.amount_minor, a.title):
        return _result(StepStatus.FAILED, error="Booking changed after approval; nothing was charged.")
    if b.status == "CONFIRMED":
        return _confirmed(b)
    if b.status in ("CANCELLED", "EXPIRED"):
        return _result(StepStatus.FAILED, error=f"This hold was {b.status.lower()}, so nothing was charged.")
    if b.status == "HELD" and b.expires_at <= datetime.now(UTC):
        b.status = "EXPIRED"
        return _result(StepStatus.FAILED, error="The hold expired before approval, so nothing was charged. "
                                                "Search again for today's price.")
    if a.payment_method == "razorpay":
        if not ctx.settings.razorpay_configured():
            return _result(StepStatus.FAILED, error="Payment gateway keys were removed; nothing was charged.")
        b.status = "PAYMENT_PENDING"
        intent = await payments.create_intent(
            ctx.session, ctx.settings, user_id=ctx.user_id, purpose="offer_booking", purpose_ref=str(b.id),
            amount_minor=b.amount_minor, currency="INR", description=f"{a.merchant}: {b.offer['title']}"[:200],
            idempotency_key=ctx.idempotency_key)
        if ctx.start_payment_workflow is not None:
            await ctx.start_payment_workflow(str(intent.id))
        return _result(StepStatus.SUBMITTED_EXTERNALLY, {
            "payment_pending": True, "intent_id": str(intent.id), "payment_url": intent.payment_url,
            "amount_minor": intent.amount_minor, "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
            "title": b.offer["title"], "gateway": "Razorpay"})
    ref = "no-payment" if b.amount_minor == 0 else f"sandbox-pay-{secrets.token_hex(5)}"
    done = await confirm_booking(ctx.session, b.id, ctx.user_id, ref)
    check = await ctx.session.get(SandboxBooking, done.id)  # read back
    if check is None or check.status != "CONFIRMED":
        return _result(StepStatus.FAILED, error="Booking could not be verified.")
    return _confirmed(check)


def _confirmed(b: Any) -> ExecResult:
    o = b.offer
    return _result(StepStatus.CONFIRMED_BY_PROVIDER, {
        "booking_code": b.booking_code, "payment_ref": b.payment_ref, "amount_minor": b.amount_minor,
        "vertical": b.vertical, "title": o["title"], "subtitle": o["subtitle"], "source_name": o["source_name"],
        "when": o.get("when"), "conditions": o.get("conditions") or {}, "link": o.get("link"), "eta": o.get("eta"),
        "environment": "sandbox", "test": True})


def refund_for(b: Any, now: datetime) -> tuple[int, str]:
    """(refund in paise, policy text) under the offer's own cancellation terms."""
    paid = b.amount_minor
    cond = (b.offer.get("conditions") or {}).get("cancellation", "")
    when = datetime.fromisoformat(b.offer["when"]) if b.offer.get("when") else None
    if b.status in ("HELD", "PAYMENT_PENDING") or paid == 0:
        return 0, "Nothing was paid."
    if when and when <= now:
        return -1, "It has already started, so it can't be cancelled."
    low = cond.lower()
    if "non-refundable" in low:
        return 0, cond
    if m := re.search(r"cancellation fee ₹([\d,]+)", cond):
        fee = int(m.group(1).replace(",", "")) * 100 * int(b.offer.get("qty") or 1)
        return max(paid - fee, 0), cond
    if m := re.search(r"free cancellation until (\d{1,2} \w{3})", low):
        until = datetime.strptime(f"{m.group(1)} {when.year if when else now.year}", "%d %b %Y").replace(
            hour=23, minute=59, tzinfo=IST)
        return (paid, cond) if now <= until else (0, cond)
    if "12 h before" in low and when:
        return (paid, cond) if when - now >= timedelta(hours=12) else (paid // 2, cond)
    if "restaurant accepts" in low:
        return 0, "The restaurant has already accepted the order."
    if b.vertical == "product":
        return paid, "Cancelled before dispatch: full refund."
    return paid, cond or "Full refund."


async def exec_cancel_booking(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import CancelBookingArgs
    from apna.db.models import SandboxBooking

    a = CancelBookingArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    stmt = select(SandboxBooking).where(SandboxBooking.user_id == ctx.user_id)
    if a.ref:
        stmt = stmt.where(SandboxBooking.booking_code == a.ref.strip().upper())
    else:
        stmt = stmt.where(SandboxBooking.status.in_(("HELD", "PAYMENT_PENDING", "CONFIRMED")))
    b = (await ctx.session.execute(stmt.order_by(SandboxBooking.created_at.desc()).limit(1)
                                   .with_for_update())).scalars().first()
    if b is None:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "no_booking", "test": True})
    if b.status == "CANCELLED":
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "already_cancelled", "title": b.offer["title"],
                                                      "refund_minor": b.refund_minor or 0, "test": True})
    refund, why = refund_for(b, datetime.now(IST))
    if refund < 0:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "cannot_cancel", "title": b.offer["title"], "why": why,
                                                      "test": True})
    was = b.status
    b.status, b.refund_minor = "CANCELLED", refund
    return _result(StepStatus.COMPLETED_LOCALLY, {
        "kind": "booking_cancelled", "title": b.offer["title"], "code": b.booking_code, "paid_minor": b.amount_minor
        if was == "CONFIRMED" else 0, "refund_minor": refund, "policy": why, "test": True})


# ------------------------------------------------------------------ price-drop watches


async def exec_watch_price(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import WatchPriceArgs
    from apna.db.models import PriceWatch

    a = WatchPriceArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    found, meta = offers.search(offer_query(a))
    base = {"vertical": a.vertical, "request": offer_request(args, "watch_price"), "args": args, "test": True,
            "tool": "watch_price"}
    if meta.get("ambiguous"):
        return _result(StepStatus.AWAITING_USER_ACTION, {**base, "question_key": "product_choice", "clause": a.query,
                                                         "choices": meta["ambiguous"]})
    if not found:
        return _result(StepStatus.COMPLETED_LOCALLY, {**base, "kind": "no_offers", "why": ""})
    best = found[0]
    item_key = best.item_key if a.vertical in ("product", "hotel", "event") else None
    title = (meta.get("product") or best.title) if a.vertical == "product" else offer_request(args).removeprefix(
        "compare prices for ").strip()
    title = title[:200]
    w = (await ctx.session.execute(select(PriceWatch).where(
        PriceWatch.user_id == ctx.user_id, PriceWatch.title == title, PriceWatch.status == "ACTIVE"))).scalars().first()
    target = a.target_price * 100 if a.target_price else None
    if w is None:
        w = PriceWatch(user_id=ctx.user_id, vertical=a.vertical, query=args, item_key=item_key, title=title,
                       start_price_minor=best.total, last_price_minor=best.total, target_price_minor=target,
                       status="ACTIVE", last_checked_at=datetime.now(UTC),
                       ends_at=datetime.now(UTC) + timedelta(days=a.days or ctx.settings.price_watch_days))
        ctx.session.add(w)
        await ctx.session.flush()
    else:
        w.target_price_minor = target or w.target_price_minor
    await ctx.session.commit()  # the workflow's first check must see the row
    if ctx.start_price_watch_workflow is not None:
        await ctx.start_price_watch_workflow(str(w.id))
    already = target is not None and best.total <= target
    return _result(StepStatus.SCHEDULED, {
        **base, "kind": "watching", "watch_id": str(w.id), "title": title, "current_minor": best.total,
        "current_source": best.source_name, "target_minor": target, "ends_at": w.ends_at.isoformat(),
        "already_below": already, "link": best.link})


async def check_watch(session: Any, watch_id: uuid.UUID, now: datetime | None = None) -> dict[str, Any]:
    """One price check. Returns {"done": bool, "notify": text | None}."""
    from apna.agent.tools import WatchPriceArgs
    from apna.db.models import PriceWatch

    w = (await session.execute(select(PriceWatch).where(PriceWatch.id == watch_id).with_for_update())).scalar_one()
    now = now or datetime.now(UTC)
    if w.status != "ACTIVE":
        return {"done": True, "notify": None}
    if now >= w.ends_at:
        w.status = "ENDED"
        return {"done": True, "notify": f"🔔 Stopped watching {w.title}: no drop below "
                                        f"{offers.rupees(w.target_price_minor or w.start_price_minor)} in time. "
                                        "Say 'alert me' again to keep watching."}
    best = offers.current_best(offer_query(WatchPriceArgs.model_validate(w.query)), w.item_key, now)
    w.last_checked_at = now
    if best is None:
        return {"done": False, "notify": None}
    w.last_price_minor = best.total
    hit = best.total <= w.target_price_minor if w.target_price_minor else best.total <= int(w.start_price_minor * 0.97)
    if not hit:
        return {"done": False, "notify": None}
    w.status = "TRIGGERED"
    drop = w.start_price_minor - best.total
    return {"done": True, "notify": (
        f"📉 Price drop (TEST prices): {w.title} is now {offers.rupees(best.total)} on {best.source_name}"
        + (f", {offers.rupees(drop)} less than when you asked" if drop > 0 else "")
        + (f" (your target {offers.rupees(w.target_price_minor)})" if w.target_price_minor else "")
        + f".\nCheck it: {best.link}\nSay \"compare {w.title}\" to book it here.")}


async def exec_manage_price_watch(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import ManageWatchArgs
    from apna.db.models import PriceWatch

    a = ManageWatchArgs.model_validate(args)
    rows = (await ctx.session.execute(select(PriceWatch).where(
        PriceWatch.user_id == ctx.user_id, PriceWatch.status == "ACTIVE").order_by(PriceWatch.created_at.desc())
    )).scalars().all()
    if a.action == "stop":
        name = (a.name or "").lower()
        hit = [w for w in rows if not name or any(t in w.title.lower() for t in name.split() if len(t) > 2)]
        for w in hit:
            w.status = "CANCELLED"
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "watches_stopped", "titles": [w.title for w in hit]})
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "watches", "items": [
        {"title": w.title, "start": offers.rupees(w.start_price_minor), "last": offers.rupees(w.last_price_minor),
         "target": offers.rupees(w.target_price_minor) if w.target_price_minor else None,
         "ends": w.ends_at.astimezone(IST).strftime("%d %b")} for w in rows]})


# ------------------------------------------------------------------ shopping list


def _list_text(rows: list[Any]) -> list[str]:
    out = []
    for r in rows:
        amount = r.size or (f"× {r.qty}" if r.qty > 1 else "")
        out.append(" ".join(x for x in (r.brand or "", r.name, amount) if x))
    return out


async def exec_shopping_list(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import ShoppingListArgs
    from apna.db.models import ShoppingListItem

    a = ShoppingListArgs.model_validate(args)
    s = ctx.session
    rows = list((await s.execute(select(ShoppingListItem).where(ShoppingListItem.user_id == ctx.user_id)
                                 .order_by(ShoppingListItem.created_at))).scalars())
    changed: list[str] = []

    def find(name: str) -> Any:
        n = name.strip().lower()
        return next((r for r in rows if r.name.lower() == n or r.name.lower().rstrip("s") == n.rstrip("s")), None)

    if a.action == "add":
        for i in a.items:
            r = find(i.name)
            if r is None:
                r = ShoppingListItem(user_id=ctx.user_id, name=i.name.strip()[:80], qty=i.qty, size=i.size, brand=i.brand)
                s.add(r)
                rows.append(r)
            else:
                r.qty = r.qty + i.qty if not (i.size or r.size) else r.qty
                r.size = i.size or r.size
                r.brand = i.brand or r.brand
            changed.append(i.name)
    elif a.action == "remove":
        for i in a.items:
            r = find(i.name)
            if r is not None:
                await s.delete(r)
                rows.remove(r)
                changed.append(r.name)
    elif a.action == "clear":
        for r in rows:
            await s.delete(r)
        changed, rows = [r.name for r in rows], []
    await s.flush()
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "list", "action": a.action, "changed": changed,
                                                  "items": _list_text(rows)})


async def exec_order_shopping_list(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.commerce import _build_cart
    from apna.agent.tools import OrderGroceriesArgs, OrderListArgs
    from apna.db.models import ShoppingListItem

    a = OrderListArgs.model_validate(args)
    rows = list((await ctx.session.execute(select(ShoppingListItem).where(ShoppingListItem.user_id == ctx.user_id)
                                           .order_by(ShoppingListItem.created_at))).scalars())
    if not rows:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "list_empty"})
    o = OrderGroceriesArgs.model_validate({
        "items": [{"name": r.name, "qty": r.qty, "size": r.size, "brand": r.brand} for r in rows[:25]],
        **a.model_dump()})
    return await _build_cart(ctx, o, o.model_dump())


# ------------------------------------------------------------------ web search, inbox


async def exec_web_search(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    import httpx

    from apna.agent.tools import WebSearchArgs
    from apna.providers import websearch

    a = WebSearchArgs.model_validate(args)
    kind = a.kind or websearch.classify(a.query)
    try:
        ans = await websearch.search(ctx.settings, a.query, kind)
    except (httpx.HTTPError, ValueError) as exc:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "web_unavailable", "query": a.query,
                                                      "error": type(exc).__name__})
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "web", "query": a.query, "search_kind": kind,
                                                  **websearch.to_dict(ans),
                                                  "ipo_official": websearch.IPO_OFFICIAL if kind == "ipo" else []})


async def exec_read_inbox(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import ReadInboxArgs

    a = ReadInboxArgs.model_validate(args)
    threads = await ctx.email.search_threads(ctx.session, ctx.user_id, a.query or "latest")
    return _result(StepStatus.COMPLETED_LOCALLY, {
        "kind": "inbox", "provider": ctx.email.name, "environment": ctx.email.environment,
        "threads": [{"from": t.from_name or t.from_address, "address": t.from_address, "subject": t.subject,
                     "snippet": t.snippet, "received": t.received_at} for t in threads[:5]]})


# ------------------------------------------------------------------ Google


async def exec_connect_google(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.providers import google

    if not ctx.settings.google_configured():
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_setup_needed"})
    if await google.connected(ctx.session, ctx.user_id):
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_already"})
    url = await google.start_connect(ctx.session, ctx.settings, ctx.user_id)
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_link", "url": url, "minutes": 10})


async def exec_disconnect_google(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.services import accounts

    done = await accounts.revoke(ctx.session, ctx.user_id, "google")
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_disconnected" if done else "google_not_connected"})


async def _google_ready(ctx: ToolContext) -> ExecResult | None:
    from apna.providers import google

    if not ctx.settings.google_configured():
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_setup_needed"})
    if not await google.connected(ctx.session, ctx.user_id):
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_not_connected"})
    return None


async def exec_calendar_list(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import CalendarListArgs
    from apna.providers import google

    a = CalendarListArgs.model_validate(args)
    if (blocked := await _google_ready(ctx)) is not None:
        return blocked
    tz = ZoneInfo(ctx.profile.timezone if ctx.profile else "Asia/Kolkata")
    day = date.fromisoformat(a.day) if a.day else datetime.now(tz).date()
    start = datetime.combine(day, datetime.min.time(), tz)
    try:
        events = await google.list_events(ctx.session, ctx.settings, ctx.user_id, start, start + timedelta(days=a.days))
    except google.GoogleNotConnected:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_not_connected"})
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "calendar", "day": day.isoformat(), "days": a.days,
                                                  "events": events})


async def exec_calendar_create_event(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import CalendarCreateArgs
    from apna.providers import google

    a = CalendarCreateArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    if (blocked := await _google_ready(ctx)) is not None:
        return blocked
    try:
        ev = await google.create_event(ctx.session, ctx.settings, ctx.user_id, title=a.title, start=a.start_local,
                                       end=a.start_local + timedelta(minutes=a.duration_min), tz=a.timezone,
                                       request_id=ctx.idempotency_key)
    except google.GoogleNotConnected:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "google_not_connected"})
    if not ev.get("event_id"):
        return _result(StepStatus.FAILED, error="Google Calendar didn't confirm the event.")
    return _result(StepStatus.CONFIRMED_BY_PROVIDER, {"kind": "event_created", **ev,
                                                      "local": a.start_local.strftime("%a %d %b, %I:%M %p")})


# ------------------------------------------------------------------ deterministic plans


_BOOK = re.compile(r"^\s*book offer (OFR-[0-9A-F]{8})\s*$", re.I)


def direct_plan(text: str) -> Any | None:
    """Plans that must not depend on a model: picking an offer from a list."""
    from apna.agent.schemas import DraftPlan, DraftStep

    if m := _BOOK.match(text):
        oid = m.group(1).upper()
        return DraftPlan(goal=text[:500], constraints=[], steps=[
            DraftStep(key="s1", tool="book_offer", description="Hold the chosen offer (TEST)", args={"offer_id": oid},
                      depends_on=[]),
            DraftStep(key="s2", tool="pay_offer", description="Pay for the chosen offer",
                      args={"booking_id": "{{s1.booking_id}}"}, depends_on=["s1"])])
    return None
