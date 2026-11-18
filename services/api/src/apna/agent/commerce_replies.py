"""Replies for grocery, payment, weather, status and provider-handoff steps (rendered from step results).

Wording rules: sandbox results always say TEST; real providers' limits are stated plainly with a link to
their own app; prices show the full breakdown; nothing is called "confirmed" without a provider record.
English and Hinglish texts; Hindi/Marathi users get the English text for these new flows for now.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_GROCERY_TOOLS = {"order_groceries", "update_grocery_cart", "cancel_grocery_order", "pay_grocery_order",
                  "get_order_status", "get_weather", "provider_handoff", "order_shopping_list"}
_ICON = {"ride": "🚕", "food": "🍽️", "home": "🏠", "travel": "🏨", "bus": "🚌", "grocery": "🛒"}


def _hl(lang: str) -> bool:
    return lang in ("hi-Latn", "mr-Latn")


def _rs(minor: int) -> str:
    from apna.providers.grocery import rupees

    return rupees(int(minor))


def _local(iso: str | None, fmt: str = "%I:%M %p") -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(ZoneInfo("Asia/Kolkata")).strftime(fmt).lstrip("0")


def render_commerce(s: dict[str, Any], lang: str, steps: list[dict[str, Any]]) -> tuple[list[str], list[dict]] | None:
    status, tool, r = s["status"], s["tool"], s.get("result") or {}
    if tool not in _GROCERY_TOOLS:
        return None
    hl = _hl(lang)
    lines: list[str] = []
    actions: list[dict[str, Any]] = []
    kind = r.get("kind")

    if status == "AWAITING_USER_ACTION" and r.get("question_key"):
        key = r["question_key"]
        if key == "store_choice":
            head = "🧪 TEST · Kaunsa app? Total (fees ke saath):" if hl else "🧪 TEST · Which app? Totals incl. fees:"
        elif key == "variant_choice":
            head = (f"🧪 TEST · Kaunsa {r.get('item')}? (sasta wala chahiye to 'sasta' likhiye)" if hl else
                    f"🧪 TEST · Which {r.get('item')}? (say 'cheapest' if you don't mind)")
        elif key == "substitution_choice":
            head = (f"🧪 TEST · {r.get('item')} {r.get('store')} par nahi hai. Bina poochhe badlunga nahi:" if hl else
                    f"🧪 TEST · {r.get('item')} isn't available on {r.get('store')}. I won't swap it without asking:")
        elif key == "price_changed":
            new = r.get("new_total") or "unavailable"
            head = (f"⚠️ Approve karne ke baad price badal gaya: {r.get('old_total')} → {new}. Kuch charge nahi hua." if hl
                    else f"⚠️ The price changed after you approved: {r.get('old_total')} → {new}. Nothing was charged.")
        elif key == "delivery_area_needed":
            return None  # generic question text
        else:
            return None
        lines.append(head)
        lines += [f"{i}. {c}" for i, c in enumerate(r.get("choices") or [], 1)]
        lines.append("Number ya naam likh kar reply kijiye." if hl else "Reply with a number or name.")
        return lines, actions

    if tool in ("order_groceries", "update_grocery_cart", "order_shopping_list") and status == "COMPLETED_LOCALLY":
        if kind == "list_empty":
            lines.append("📝 Your shopping list is empty. Say \"add milk and eggs to my list\" first.")
        elif kind == "cart":
            where = f" · {r['where']}" if r.get("where") else ""
            lines.append("🧪 TEST order (sandbox: asli order ya paisa nahi)" if hl
                         else "🧪 TEST order (sandbox: no real order, no real money)")
            lines.append(f"🛒 {r['store']} · ~{r['eta_min']} min{where}")
            for i, x in enumerate(r["lines"], 1):
                lines.append(f"{i}. {x['name']} {x['size']} × {x['qty']} - {_rs(x['unit_minor'] * x['qty'])}")
            fees = []
            fees.append(f"Delivery {_rs(r['delivery_fee_minor'])}" if r["delivery_fee_minor"] else "Delivery FREE")
            if r.get("handling_fee_minor"):
                fees.append(f"Handling {_rs(r['handling_fee_minor'])}")
            lines.append(f"Items {_rs(r['item_total_minor'])} · {' · '.join(fees)} · Total {_rs(r['total_minor'])}")
            if r.get("compared"):
                lines.append("Compared: " + " | ".join(r["compared"]))
            if r.get("unknown"):
                lines.append(f"⚠️ Not in the sandbox catalogue: {', '.join(r['unknown'])}")
            if r.get("quote_expires_at"):
                lines.append(f"⏳ Price held until {_local(r['quote_expires_at'])}. {r.get('cancellation', '')}")
            lines.append(f"💳 Payment: {r.get('payment_method', '')}")
            real = r.get("real") or {}
            if real:
                lines.append(f"ℹ️ Real {real['provider']}: {real['why']} Open it: {real['link']}")
        elif kind == "nothing_found":
            lines.append(f"🧪 I couldn't find {', '.join(r.get('unknown') or [])} in the sandbox catalogue. "
                         "Try common names like milk, atta, paneer or eggs.")
        elif kind == "not_serviceable":
            lines.append("🧪 TEST · None of these apps can take this order:")
            lines += [f"• {x}" for x in r.get("reasons") or []]
            for real in r.get("real") or []:
                if real:
                    lines.append(f"ℹ️ {real['provider']}: {real['link']}")
        elif kind == "no_cart":
            lines.append("🛒 There's no open cart to change. Tell me what to order." if not hl
                         else "🛒 Koi open cart nahi hai. Kya mangwana hai bataiye.")
        elif kind == "cart_emptied":
            lines.append("🛒 The cart is empty now, so I cancelled it. Nothing was charged.")
        return lines, actions

    if tool == "cancel_grocery_order" and status == "COMPLETED_LOCALLY":
        lines.append("✋ Cancelled your open cart. Nothing was charged." if kind == "cancelled"
                     else "🛒 There's no open cart to cancel.")
        return lines, actions

    if tool == "pay_grocery_order":
        if status == "AWAITING_APPROVAL":
            method = ("Razorpay link: you pay in your own UPI app" if r.get("payment_method") == "razorpay"
                      else "TEST payment, nothing is charged")
            to = f" to {r['deliver_to']}" if r.get("deliver_to") else ""
            lines.append(f"💳 Approve {_rs(int(r.get('amount_minor') or 0))} for {r.get('store', 'this order')}{to}? "
                         f"({method})")
            actions.append({"type": "approval", "approval_id": s.get("approval_id"), "step_key": s["key"]})
        elif status == "SUBMITTED_EXTERNALLY" and r.get("payment_pending"):
            lines.append(f"💳 Pay {_rs(r['amount_minor'])} here (UPI: GPay / PhonePe / Paytm, or card): {r['payment_url']}")
            lines.append(f"The link expires at {_local(r.get('expires_at'))}. I'll confirm once Razorpay confirms the "
                         "payment; nothing is ordered before that.")
        elif status == "CONFIRMED_BY_PROVIDER":
            lines.append(f"✅ TEST order placed · ID {r['order_code']}")
            lines.append(f"🛒 {r['store']} · arriving in ~{r['eta_min']} min (simulated)")
            lines.append(f"💰 Paid {_rs(r['total_minor'])} (test payment {r['payment_ref']}; nothing charged). "
                         f"Sandbox order: nothing will be delivered. To order for real, use the {r['store']} app.")
        elif status == "REJECTED":
            lines.append("✋ Okay, order cancelled. Nothing was charged." if not hl
                         else "✋ Theek hai, order cancel. Koi paisa nahi kata.")
        elif status == "SKIPPED":
            return [], []
        else:
            return None
        return lines, actions

    if tool == "get_order_status" and status == "COMPLETED_LOCALLY":
        items = r.get("items") or []
        if not items:
            lines.append("📦 I don't see any orders, bookings or payments for you yet.")
        else:
            lines.append("📦 Your recent orders, bookings and payments:")
            for x in items:
                test = " (TEST)" if x.get("test") else ""
                lines.append(f"• {x['kind'].title()} {x['ref']} · {x['store']} · {x['status']} · {x['total']}{test}")
        return lines, actions

    if tool == "get_weather" and status == "COMPLETED_LOCALLY":
        if kind == "place_not_found":
            lines.append(f"🌍 I couldn't find a place called '{r.get('place')}'. Try the city name.")
            return lines, actions
        u = r.get("units") or {}
        tu, wu = u.get("temperature", "°C"), u.get("wind", "km/h")
        lines.append(f"🌤️ {r['place']}: now {r['temperature']}{tu}"
                     + (f" (feels {r['feels_like']}{tu})" if r.get("feels_like") is not None else "")
                     + f", {r['conditions']}"
                     + (f", humidity {r['humidity']}%" if r.get("humidity") is not None else "")
                     + (f", wind {r['wind_kmh']} {wu}" if r.get("wind_kmh") is not None else ""))
        for d in r.get("days") or []:
            mark = "👉 " if d["day"] == r.get("focus_day") else ""
            day = datetime.fromisoformat(d["day"]).strftime("%a %d %b")
            rain = f", rain chance {d['rain']}%" if d.get("rain") is not None else ""
            lines.append(f"{mark}{day}: {d['min']}–{d['max']}{tu}, {d['conditions']}{rain}")
        lines.append(f"Source: {r['source']} · updated {r['observed_at'].replace('T', ' ')} ({r['timezone']})")
        return lines, actions

    if tool == "provider_handoff" and status == "COMPLETED_LOCALLY":
        icon = _ICON.get(r.get("domain", ""), "🔗")
        if r.get("summary"):
            lines.append(f"{icon} {r['summary']}")
        lines.append("I can't book this inside the chat yet. Here's exactly what each app allows:" if not hl else
                     "Yeh abhi chat ke andar book nahi ho sakta. Har app ke saath kya possible hai:")
        for p in r.get("providers") or []:
            note = " (compares prices, doesn't take bookings)" if p.get("kind") == "compare" else ""
            lines.append(f"• {p['provider']}{note}: {p['why']} Open: {p['link']}")
        return lines, actions
    return None
