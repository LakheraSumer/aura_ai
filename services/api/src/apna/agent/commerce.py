"""Commerce, weather and provider-handoff executors (grocery sandbox, payments, live weather).

Every grocery reply says TEST: the real apps (Blinkit, Zepto, Instamart, BigBasket, Flipkart Minutes) don't
offer ordering to outside apps (docs/integrations/CAPABILITY_MATRIX.md), so the cart is built against the
sandbox and the reply also carries the real provider's status and an app link.

Questions are asked instead of guessing: product line ("which milk?"), substitutions, store choice when
the user didn't say how to choose, and renewed approval when the price changed after approval.
"""

from __future__ import annotations

import difflib
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apna.agent import policy
from apna.domain.enums import StepStatus

if TYPE_CHECKING:
    from apna.agent.executors import ExecResult, ToolContext

GROCERY_QUESTIONS = frozenset({"store_choice", "variant_choice", "substitution_choice", "price_changed"})

# sandbox store id for each real provider id
_SANDBOX_STORE = {"blinkit": "blinkit", "zepto": "zepto", "instamart": "instamart", "bigbasket": "bbnow",
                  "flipkart_minutes": "flipkart_minutes"}
_REAL_PROVIDER = {v: k for k, v in _SANDBOX_STORE.items()}


def _result(status: StepStatus, data: dict[str, Any] | None = None, error: str | None = None) -> ExecResult:
    from apna.agent.executors import ExecResult

    return ExecResult(status, data or {}, error)


# ------------------------------------------------------------------ request text (for merging answers)


def grocery_request(args: dict[str, Any]) -> str:
    """The order in words, so a follow-up answer can be merged and re-planned."""
    items = []
    for i in args.get("items") or []:
        bits = []
        if i.get("size"):
            bits.append(str(i["size"]))
        elif i.get("qty") and int(i["qty"]) > 1:
            bits.append(f"{i['qty']} x")
        if i.get("brand"):
            bits.append(str(i["brand"]))
        bits.append(str(i.get("name")))
        items.append(" ".join(bits))
    text = "order " + ", ".join(items)
    stores = args.get("stores") or ([args["store"]] if args.get("store") else [])
    if stores:
        text += " from " + " or ".join(stores)
    if args.get("choose"):
        text += f", choose the {args['choose']} total"
    where = ", ".join(x for x in (args.get("area"), args.get("city")) if x)
    return text + (f", deliver to {where}" if where else "")


def _pick(choices: list[str], answer: str) -> str | None:
    ans = answer.strip().lower()
    if m := re.fullmatch(r"(?:option\s*|no\.?\s*|#)?(\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:wala|vala|one))?", ans):
        i = int(m.group(1))
        return choices[i - 1] if 1 <= i <= len(choices) else None
    ords = {"first": 1, "pehla": 1, "second": 2, "dusra": 2, "doosra": 2, "third": 3, "teesra": 3}
    for k, v in ords.items():
        if re.search(rf"\b{k}\b", ans) and v <= len(choices):
            return choices[v - 1]
    hit = [c for c in choices if ans and ans in c.lower()]
    if len(hit) == 1:
        return hit[0]
    close = difflib.get_close_matches(ans, [c.lower() for c in choices], n=1, cutoff=0.6)
    return next((c for c in choices if close and c.lower() == close[0]), None)


def resolve_store_choice(args: dict[str, Any], choices: list[str], answer: str) -> str | None:
    """'2' / 'zepto' / 'sabse sasta' / 'cheaper one' / 'jaldi wala' -> the order with that store."""
    from apna.providers import grocery

    ans = answer.strip().lower()
    usable = [c for c in choices if "not available" not in c]
    store = None
    if re.search(r"cheap|sasta|sasti|\bkam\b|lowest|best|kam wala", ans) and usable:
        store = grocery.store_named(usable[0].split(" - ")[0])  # options are sorted cheapest-first
    elif re.search(r"fast|jaldi|quick|sabse jaldi", ans) and usable:
        fastest = min(usable, key=lambda c: int(m.group(1)) if (m := re.search(r"~(\d+) min", c)) else 999)
        store = grocery.store_named(fastest.split(" - ")[0])
    else:
        chosen = _pick(choices, answer)
        store = grocery.store_named(chosen.split(" - ")[0]) if chosen else grocery.store_named(ans)
    if store is None:
        return None
    return grocery_request({**args, "store": store.name, "stores": None, "choose": None})


def resolve_grocery_answer(key: str, r: dict[str, Any], answer: str) -> str | None:
    """Deterministic merge for grocery questions; None -> let the planner re-plan with the options shown."""
    args, choices = dict(r.get("args") or {}), list(r.get("choices") or [])
    if key == "store_choice":
        return resolve_store_choice(args, choices, answer)
    if key == "variant_choice":
        item = r.get("item")
        ans = answer.strip().lower()
        if re.search(r"cheap|sasta|sasti|koi bhi|any", ans):
            line = r.get("cheapest_line")
        else:
            chosen = _pick(choices, answer)
            line = chosen.split(" (")[0] if chosen else None
        if not line:
            return None
        args["items"] = [{**i, "brand": line} if i.get("name") == item else i for i in args.get("items") or []]
        return grocery_request(args)
    if key == "substitution_choice":
        chosen = _pick(choices, answer)
        if chosen is None:
            return None
        item = r.get("item")
        if chosen.startswith("Use "):
            line = chosen.removeprefix("Use ").split(" instead")[0]
            args["items"] = [{**i, "brand": line} if i.get("name") == item else i for i in args.get("items") or []]
        elif chosen.startswith("Skip "):
            args["items"] = [i for i in args.get("items") or [] if i.get("name") != item]
        elif chosen.startswith("Try "):
            args.update(store=chosen.removeprefix("Try ").split(" (")[0], stores=None, choose=None)
        return grocery_request(args) if args.get("items") else None
    if key == "price_changed":
        if re.search(r"^(1|yes|haan|ha|ok|okay|pay|confirm)\b", answer.strip().lower()):
            return grocery_request(args)  # re-plan -> new cart at today's price -> new approval
        return None
    return None


# ------------------------------------------------------------------ order / update / status / cancel


def _real_note(store_id: str, first_item: str | None) -> dict[str, Any]:
    from apna.providers import registry

    pid = _REAL_PROVIDER.get(store_id, store_id)
    spec = registry.REGISTRY.get(pid)
    if spec is None:
        return {}
    st = registry.status(pid, "create_cart", registry.env_snapshot())
    return {"provider": spec.name, "status": st.value, "why": registry.explain(spec, "create_cart", st),
            "link": registry.handoff_link(pid, {"query": first_item}) or spec.homepage}


async def _build_cart(ctx: ToolContext, a: Any, raw: dict[str, Any]) -> ExecResult:
    from apna.providers import grocery
    from apna.providers import grocery_quote as gq

    reqs = [gq.make_request(i.name, i.qty, i.size, i.brand) for i in a.items]
    where = ", ".join(x for x in (a.area, a.city) if x)
    base = {"request": grocery_request(raw), "args": raw, "where": where, "test": True}
    unknown = [r.raw for r in reqs if r.group is None]
    known = [r for r in reqs if r.group is not None]
    if not known:
        return _result(StepStatus.COMPLETED_LOCALLY, {**base, "kind": "nothing_found", "unknown": unknown})

    named = [s for s in ([a.store] if a.store else []) + list(a.stores or []) if s]
    store_ids = []
    for s in named:
        st = grocery.store_named(s)
        if st and st.id not in store_ids:
            store_ids.append(st.id)
    if named and not store_ids:
        store_ids = list(grocery.STORES)  # unknown names: compare everything rather than guess

    def ask(key: str, choices: list[str], **extra: Any) -> ExecResult:
        return _result(StepStatus.AWAITING_USER_ACTION, {**base, "question_key": key, "clause": base["request"],
                                                         "choices": choices[:6], **extra})

    for r in known:  # which product? (never guessed)
        lines = gq.ambiguous(r)
        if lines:
            def unit_price(line: str, r: gq.Request = r) -> str:
                ps = [p for p in grocery._GROUPS[r.group][2] if p.name == line]  # type: ignore[index]
                return ", ".join(f"{p.size} {grocery.rupees(p.price_minor)}" for p in ps)
            cheapest = min(lines, key=lambda ln: min(p.price_minor / max(gq.product_amount(p)[0], 1)
                                                     for p in grocery._GROUPS[r.group][2] if p.name == ln))  # type: ignore[index]
            return ask("variant_choice", [f"{ln} ({unit_price(ln)})" for ln in lines], item=r.raw,
                       cheapest_line=cheapest)

    quotes = gq.compare(known, a.city, store_ids or None)
    eligible = [q for q in quotes if q.eligible]
    if not eligible:
        partial = [q for q in quotes if not q.reasons and q.lines and q.missing]
        if partial:
            q = partial[0]
            item = q.missing[0]
            req = next(r for r in known if r.raw == item)
            others = [ln for ln in gq.lines_of(req.group) if ln != req.line  # type: ignore[arg-type]
                      and gq.plan_packs(q.store, req, ln)]
            elsewhere = [x.store.name for x in quotes if x is not q and not x.reasons and item not in x.missing]
            choices = [f"Use {ln} instead" for ln in others[:2]] + [f"Skip {item}"]
            choices += [f"Try {s} (has {item})" for s in elsewhere[:2]]
            return ask("substitution_choice", choices, item=item, store=q.store.name)
        return _result(StepStatus.COMPLETED_LOCALLY, {
            **base, "kind": "not_serviceable", "reasons": [gq.option_text(q) for q in quotes],
            "real": [_real_note(q.store.id, known[0].raw) for q in quotes[:2]]})

    if len(store_ids) == 1 or a.choose == "cheapest" or len(eligible) == 1:
        chosen = eligible[0]
    elif a.choose == "fastest":
        chosen = min(eligible, key=lambda q: (q.store.eta_min, q.total))
    else:
        return ask("store_choice", [gq.option_text(q) for q in quotes])

    req_dicts = [r.to_args() for r in known]
    order = await grocery.save_cart(ctx.session, user_id=ctx.user_id, idempotency_key=ctx.idempotency_key,
                                    quote=chosen, requests=req_dicts, city=a.city or "", area=a.area,
                                    ttl_minutes=ctx.settings.quote_ttl_minutes)
    compared = [gq.option_text(q) for q in quotes] if len(quotes) > 1 else []
    return _result(StepStatus.COMPLETED_LOCALLY, {
        **base, "kind": "cart", "order_id": str(order.id), "store": chosen.store.name, "eta_min": chosen.store.eta_min,
        "lines": order.items, "item_total_minor": order.item_total_minor,
        "delivery_fee_minor": order.delivery_fee_minor, "handling_fee_minor": order.handling_fee_minor,
        "total_minor": order.total_minor, "compared": compared, "unknown": unknown,
        "quote_expires_at": order.quote_expires_at.isoformat() if order.quote_expires_at else None,
        "cancellation": grocery.CANCELLATION_TERMS,
        "payment_method": "Razorpay payment link (UPI: GPay / PhonePe / Paytm, or card)"
        if ctx.settings.razorpay_configured() else "TEST sandbox payment (nothing is charged)",
        "real": _real_note(chosen.store.id, known[0].raw),
    })


async def exec_order_groceries(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import OrderGroceriesArgs

    a = OrderGroceriesArgs.model_validate(args)
    return await _build_cart(ctx, a, a.model_dump())


async def exec_update_grocery_cart(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    """'add two milk', 'remove bread', 'make eggs 12': edits the user's latest open cart into a new cart
    (new approval; the old cart is replaced so its Pay button can't be used)."""
    from apna.agent.tools import OrderGroceriesArgs, UpdateCartArgs
    from apna.providers import grocery
    from apna.providers import grocery_quote as gq

    a = UpdateCartArgs.model_validate(args)
    cart = await grocery.latest_cart(ctx.session, ctx.user_id)
    if cart is None:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "no_cart", "test": True})
    items = [dict(x) for x in (cart.request or [])]
    for ch in a.changes:
        group = grocery.find_group(ch.name)
        idx = next((k for k, i in enumerate(items) if grocery.find_group(i["name"]) == group), None) if group else None
        if ch.action == "remove":
            if idx is not None:
                items.pop(idx)
        elif ch.action == "add" and idx is not None:
            cur = items[idx]
            add = gq.make_request(ch.name, ch.qty or 1, ch.size, ch.brand)
            have = gq.make_request(cur["name"], cur.get("qty") or 1, cur.get("size"), cur.get("brand"))
            if have.amount is not None and add.amount is not None and have.unit == add.unit:
                cur["size"] = f"{have.amount + add.amount:g} {have.unit}" if have.unit != "pcs" else \
                    f"{int(have.amount + add.amount)}"
            elif have.amount is None and add.amount is None:
                cur["qty"] = int(cur.get("qty") or 1) + (ch.qty or 1)
            else:
                items.append({"name": ch.name, "qty": ch.qty or 1, "size": ch.size, "brand": ch.brand})
        elif ch.action in ("add", "set"):
            new = {"name": ch.name, "qty": ch.qty or 1, "size": ch.size, "brand": ch.brand or (
                items[idx].get("brand") if idx is not None else None)}
            if idx is not None:
                items[idx] = new
            else:
                items.append(new)
    if not items:
        await grocery.cancel_order(ctx.session, cart.id, ctx.user_id)
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "cart_emptied", "test": True})
    store = grocery.STORES[cart.store]
    o = OrderGroceriesArgs.model_validate({"items": items, "store": store.name, "city": cart.city or None,
                                           "area": cart.area})
    return await _build_cart(ctx, o, o.model_dump())


async def exec_get_order_status(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    """'what happened to my order/booking?' — verified from Apna's provider records, newest first."""
    from sqlalchemy import select

    from apna.db.models import PaymentIntent, SandboxCinemaBooking, SandboxGroceryOrder
    from apna.providers import grocery

    s = ctx.session
    orders = (await s.execute(select(SandboxGroceryOrder).where(SandboxGroceryOrder.user_id == ctx.user_id)
                              .order_by(SandboxGroceryOrder.created_at.desc()).limit(3))).scalars().all()
    bookings = (await s.execute(select(SandboxCinemaBooking).where(SandboxCinemaBooking.user_id == ctx.user_id)
                                .order_by(SandboxCinemaBooking.created_at.desc()).limit(2))).scalars().all()
    intents = (await s.execute(select(PaymentIntent).where(PaymentIntent.user_id == ctx.user_id)
                               .order_by(PaymentIntent.created_at.desc()).limit(3))).scalars().all()
    now = datetime.now(UTC)
    items = []
    orders = [o for o in orders if o.status != "REPLACED"]  # superseded carts are noise
    for o in orders:
        st = o.status
        if st == "PLACED" and o.placed_at:
            mins = (now - o.placed_at).total_seconds() / 60
            st = "DELIVERED (simulated)" if mins >= o.eta_minutes else f"ON THE WAY (simulated, ~{max(1, int(o.eta_minutes - mins))} min)"
        items.append({"kind": "grocery", "ref": o.order_code or str(o.id)[:8], "store": grocery.STORES[o.store].name,
                      "status": st, "total": grocery.rupees(o.total_minor), "test": True})
    for b in bookings:
        items.append({"kind": "movie", "ref": b.booking_code or str(b.id)[:8], "store": b.theatre, "status": b.status,
                      "total": grocery.rupees(b.amount_minor), "test": True})
    from apna.db.models import SandboxBooking

    offers_b = (await s.execute(select(SandboxBooking).where(SandboxBooking.user_id == ctx.user_id,
                                                             SandboxBooking.status != "EXPIRED")
                                .order_by(SandboxBooking.created_at.desc()).limit(4))).scalars().all()
    for b in offers_b:
        st = b.status
        if st == "CONFIRMED" and b.vertical in ("product", "food") and b.confirmed_at:
            mins = (now - b.confirmed_at).total_seconds() / 60
            eta = 40 if b.vertical == "food" else 60 * 24 * 2
            st = "DELIVERED (simulated)" if mins >= eta else "ON THE WAY (simulated)"
        if st == "CANCELLED" and b.refund_minor:
            st = f"CANCELLED · refund {grocery.rupees(b.refund_minor)}"
        items.append({"kind": b.vertical, "ref": b.booking_code or str(b.id)[:8], "store":
                      f"{b.offer['title'][:60]} via {b.offer['source_name']}", "status": st,
                      "total": grocery.rupees(b.amount_minor), "test": True})
    for p in intents:
        items.append({"kind": "payment", "ref": (p.gateway_ref or str(p.id)[:8]), "store": p.gateway,
                      "status": p.status, "total": grocery.rupees(p.amount_minor), "test": p.gateway == "sandbox"})
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "status", "items": items})


async def exec_cancel_grocery_order(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.providers import grocery

    cart = await grocery.latest_cart(ctx.session, ctx.user_id)
    if cart is None:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "no_cart", "test": True})
    status = await grocery.cancel_order(ctx.session, cart.id, ctx.user_id)
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "cancelled", "status": status, "test": True})


# ------------------------------------------------------------------ payment


async def prepare_grocery_payment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the exact purchase so the approval binds to it (store, items, address, amount, method)."""
    from apna.agent.tools import PayGroceryArgs
    from apna.db.models import SandboxGroceryOrder
    from apna.providers import grocery

    a = PayGroceryArgs.model_validate(args)
    order = await ctx.session.get(SandboxGroceryOrder, uuid.UUID(a.order_id))
    if order is None or order.user_id != ctx.user_id:
        raise ValueError("Cart not found.")
    store = grocery.STORES[order.store]
    return {"order_id": a.order_id, "amount_minor": order.total_minor, "currency": "INR",
            "merchant": f"{store.name} {grocery.MERCHANT_SUFFIX}", "store": store.name,
            "items": [f"{x['name']} {x['size']} x {x['qty']}" for x in order.items],
            "deliver_to": ", ".join(x for x in (order.area, order.city) if x),
            "payment_method": "razorpay" if ctx.settings.razorpay_configured() else "sandbox"}


async def exec_pay_grocery_order(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from apna.agent.tools import PayGroceryArgs
    from apna.db.models import SandboxGroceryOrder
    from apna.providers import grocery, payments

    a = PayGroceryArgs.model_validate(args)
    await policy.assert_authorized(ctx.session, step_id=ctx.step_id, spec=ctx.spec, args=args, profile=ctx.profile)
    order = await ctx.session.get(SandboxGroceryOrder, uuid.UUID(a.order_id))
    if order is None:
        return _result(StepStatus.FAILED, error="Cart not found.")
    current = [f"{x['name']} {x['size']} x {x['qty']}" for x in order.items]
    where = ", ".join(x for x in (order.area, order.city) if x)
    if (order.total_minor, current, where) != (a.amount_minor, a.items, a.deliver_to or ""):
        return _result(StepStatus.FAILED, error="Cart changed after approval; nothing was charged.")
    if order.status in ("REPLACED", "CANCELLED"):
        return _result(StepStatus.FAILED, error="This cart was replaced or cancelled, so nothing was charged. "
                                                "Use the latest cart's Pay button.")
    if order.status == "CART" and order.quote_expires_at and order.quote_expires_at <= datetime.now(UTC):
        new_total = grocery.reprice(order)
        if new_total != order.total_minor:
            # Approved price is no longer valid: never charge a different amount on an old approval.
            await grocery.cancel_order(ctx.session, order.id, ctx.user_id)
            return _result(StepStatus.AWAITING_USER_ACTION, {
                "question_key": "price_changed", "clause": "", "args": {"items": order.request or [],
                                                                       "store": grocery.STORES[order.store].name,
                                                                       "city": order.city, "area": order.area},
                "old_total": grocery.rupees(order.total_minor),
                "new_total": grocery.rupees(new_total) if new_total >= 0 else None,
                "choices": ["Yes, rebuild the cart at today's price", "Cancel"], "test": True})
    if a.payment_method == "razorpay":
        if not ctx.settings.razorpay_configured():
            return _result(StepStatus.FAILED, error="Payment gateway keys were removed; nothing was charged.")
        await grocery.mark_payment_pending(ctx.session, order.id, ctx.user_id)
        intent = await payments.create_intent(
            ctx.session, ctx.settings, user_id=ctx.user_id, purpose="grocery_order", purpose_ref=str(order.id),
            amount_minor=order.total_minor, currency="INR", description=f"{a.merchant}: {len(current)} items",
            idempotency_key=ctx.idempotency_key)
        if ctx.start_payment_workflow is not None:
            await ctx.start_payment_workflow(str(intent.id))
        return _result(StepStatus.SUBMITTED_EXTERNALLY, {
            "payment_pending": True, "intent_id": str(intent.id), "payment_url": intent.payment_url,
            "amount_minor": intent.amount_minor, "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
            "store": a.store, "gateway": "Razorpay"})
    try:
        placed = await grocery.pay_and_place(ctx.session, order.id, ctx.user_id)
    except grocery.CartReplaced:
        return _result(StepStatus.FAILED, error="This cart was replaced by a newer one, so nothing was charged.")
    check = await grocery.get_order(ctx.session, placed.order_code)  # independent read-back
    if check is None or check.status != "PLACED":
        return _result(StepStatus.FAILED, error="Order could not be verified.")
    return _result(StepStatus.CONFIRMED_BY_PROVIDER, {
        "order_code": check.order_code, "payment_ref": check.payment_ref, "total_minor": check.total_minor,
        "store": grocery.STORES[check.store].name, "eta_min": check.eta_minutes, "items": current,
        "environment": "sandbox", "test": True})


# ------------------------------------------------------------------ weather (live) and handoffs


async def exec_get_weather(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    from datetime import date as _date

    from apna.agent.tools import WeatherArgs
    from apna.providers.weather import OpenMeteo

    a = WeatherArgs.model_validate(args)
    om = OpenMeteo(ctx.settings.weather_base_url, ctx.settings.geocoding_base_url)
    place = await om.geocode(a.place)
    if place is None:
        return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "place_not_found", "place": a.place})
    w = await om.forecast(place)
    want = _date.fromisoformat(a.day) if a.day else None
    return _result(StepStatus.COMPLETED_LOCALLY, {
        "kind": "weather", "place": ", ".join(x for x in (place.name, place.region) if x), "timezone": w.timezone,
        "observed_at": w.observed_at_local, "temperature": w.temperature, "feels_like": w.feels_like,
        "humidity": w.humidity, "wind_kmh": w.wind_kmh, "conditions": w.conditions, "units": w.units,
        "days": [{"day": d.day.isoformat(), "min": d.t_min, "max": d.t_max, "rain": d.rain_chance,
                  "conditions": d.conditions} for d in w.days],
        "focus_day": want.isoformat() if want else None, "source": w.source})


async def exec_provider_handoff(ctx: ToolContext, args: dict[str, Any]) -> ExecResult:
    """Rides, food, home services, travel, buses: say exactly what's possible and open the right app."""
    from apna.agent.tools import HandoffArgs
    from apna.providers import registry

    a = HandoffArgs.model_validate(args)
    env = registry.env_snapshot()
    named = registry.provider_named(a.provider) if a.provider else None
    specs = [named] if named else registry.providers_for(a.domain)
    want = {"ride": "create_booking", "food": "create_cart", "home": "create_booking", "travel": "create_booking",
            "bus": "create_booking", "grocery": "create_cart"}.get(a.domain, "create_booking")
    params = {"pickup": a.pickup, "dropoff": a.dropoff, "origin": a.origin, "destination": a.destination,
              "checkin": a.checkin, "checkout": a.checkout, "adults": a.adults, "rooms": a.rooms, "query": a.query}
    out = []
    for spec in specs[:6]:
        st = registry.status(spec.id, want, env)
        out.append({"provider": spec.name, "kind": spec.kind, "status": st.value,
                    "why": registry.explain(spec, want, st), "partner": spec.partner_program,
                    "link": registry.handoff_link(spec.id, params) or spec.homepage})
    return _result(StepStatus.COMPLETED_LOCALLY, {"kind": "handoff", "domain": a.domain, "providers": out,
                                                  "summary": a.summary})
