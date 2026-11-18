"""Replies for comparisons, TEST bookings, price alerts, shopping lists, web search, inbox and Google.

Rules: sandbox prices and bookings always say TEST and link the real seller; the full price breakdown and
the conditions that matter (baggage, refunds, cancellation) are shown before approval; web results are
listed with their source and date, never rewritten as facts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

TOOLS = {"search_offers", "book_offer", "pay_offer", "cancel_booking", "watch_price", "manage_price_watch",
         "shopping_list", "web_search", "read_inbox", "connect_google", "disconnect_google", "calendar_list",
         "calendar_create_event"}
_ICON = {"product": "🛍️", "food": "🍽️", "flight": "✈️", "hotel": "🏨", "bus": "🚌", "event": "🎟️", "table": "🍴",
         "ride": "🚕"}
IST = ZoneInfo("Asia/Kolkata")


def _hl(lang: str) -> bool:
    return lang in ("hi-Latn", "mr-Latn")


def _rs(minor: int | None) -> str:
    from apna.providers.offers import rupees

    return rupees(int(minor or 0))


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(IST).strftime("%a %d %b, %I:%M %p").replace(", 0", ", ")


def _google_note(kind: str, hl: bool) -> str | None:
    return {
        "google_setup_needed": "🔑 Google isn't set up on this Apna server yet (it needs a Google Cloud OAuth client "
                               "and a fixed public URL; see docs/integrations/SETUP.md). Until then I use a TEST "
                               "mailbox and can't read your calendar.",
        "google_not_connected": "🔗 Google isn't connected. Say \"connect Google\" and I'll send a secure sign-in link "
                                "(you sign in on Google's own page; I never see your password).",
        "google_already": "✅ Google is already connected. Say \"disconnect Google\" to remove it.",
        "google_disconnected": "✅ Google disconnected. I deleted my stored access; you can also remove Apna in your "
                               "Google account's security settings.",
    }.get(kind)


def render_assist(s: dict[str, Any], lang: str, steps: list[dict[str, Any]]) -> tuple[list[str], list[dict]] | None:
    status, tool, r = s["status"], s["tool"], s.get("result") or {}
    if tool not in TOOLS:
        return None
    hl = _hl(lang)
    lines: list[str] = []
    actions: list[dict[str, Any]] = []
    kind = r.get("kind")

    if (note := _google_note(kind or "", hl)) and status == "COMPLETED_LOCALLY":
        return [note], actions

    # ---------------------------------------------------------------- comparison
    if tool in ("search_offers", "watch_price") and status == "AWAITING_USER_ACTION":
        if r.get("question_key") == "product_choice":
            lines.append("🛍️ Kaunsa exact model? Sahi product ka hi price compare karunga:" if hl else
                         "🛍️ Which exact model? I only compare prices for the identical product:")
            lines += [f"{i}. {c}" for i, c in enumerate(r.get("choices") or [], 1)]
            lines.append("Number ya naam likhiye." if hl else "Reply with a number or name.")
            return lines, actions
        if r.get("question_key") == "offer_choice":
            icon = _ICON.get(r.get("vertical", ""), "🔎")
            srcs = ", ".join(r.get("sources") or [])
            table = r.get("vertical") == "table"
            lines.append(f"{icon} 🧪 TEST prices · compared {srcs}" + (" · best discount first" if table else
                                                                       " · total incl. fees & taxes, cheapest first"))
            lines += [f"{i}. {c}" for i, c in enumerate(r.get("choices") or [], 1)]
            if r.get("save_same_minor"):
                lines.append(f"💰 Option 1 saves {_rs(r['save_same_minor'])} vs the same item on the next-cheapest site"
                             + (f" ({_rs(r['save_max_minor'])} vs the priciest option)" if r.get("save_max_minor") else ""))
            elif r.get("save_max_minor") and not table:
                lines.append(f"💰 Option 1 is {_rs(r['save_max_minor'])} cheaper than the priciest option shown.")
            lines.append("Booking ke liye number bhejiye (jaise 1 = sabse sasta). \"alert me\" = price girne par bataunga."
                         if hl else "Reply with a number to book (1 = cheapest), or \"alert me\" to watch for a price drop.")
            links = " · ".join(f"{x['source']}: {x['link']}" for x in r.get("links") or [])
            if links:
                lines.append(f"Live prices on the real sites: {links}")
            return lines, actions
    if tool == "search_offers" and status == "COMPLETED_LOCALLY":
        if kind == "estimates":
            lines.append("🚕 🧪 Fare estimates (TEST figures, cheapest first). Rides are booked in the app:")
            lines += [f"{i}. {c}" for i, c in enumerate(r.get("choices") or [], 1)]
            if r.get("save_max_minor"):
                lines.append(f"💰 Cheapest saves about {_rs(r['save_max_minor'])} vs the priciest option.")
            lines.append("Open: " + " · ".join(f"{x['source']}: {x['link']}" for x in r.get("links") or []))
        elif kind == "no_offers":
            lines.append(f"🔎 I couldn't find any {r.get('vertical', '')} offers for that in the TEST catalogue. "
                         + (r.get("why") or "Try different words, dates or places."))
        return lines, actions

    # ---------------------------------------------------------------- booking
    if tool == "book_offer" and status == "COMPLETED_LOCALLY":
        if kind == "offer_gone":
            lines.append("⚠️ That offer is no longer available at that price (prices change). Nothing was booked. "
                         + (f"Say \"{r['request']}\" to compare again." if r.get("request") else "Compare again."))
            return lines, actions
        b = r.get("breakdown") or {}
        icon = _ICON.get(r.get("vertical", ""), "🧾")
        lines.append("🧪 TEST booking (sandbox: no real ticket, order or money)")
        lines.append(f"{icon} {r['title']} · via {r['source_name']}")
        if r.get("subtitle") and r.get("vertical") != "product":
            lines.append(r["subtitle"])
        if r.get("vertical") == "table":
            lines.append("Free reservation · discount: " + (r.get("conditions") or {}).get("discount", ""))
        else:
            fees = " · ".join(f"{k.title()} {_rs(v)}" for k, v in (b.get("fees_minor") or {}).items() if v)
            parts = [f"Price {_rs(b.get('items_minor'))}"]
            if fees:
                parts.append(fees)
            if b.get("taxes_minor"):
                parts.append(f"Taxes {_rs(b['taxes_minor'])}")
            if b.get("discounts_minor"):
                parts.append(f"Discount −{_rs(b['discounts_minor'])}")
            parts.append(f"Total {_rs(r['amount_minor'])}")
            lines.append(" · ".join(parts))
        if r.get("old_total_minor"):
            lines.append(f"⚠️ The price changed since the list: {_rs(r['old_total_minor'])} → {_rs(r['amount_minor'])}.")
        cond = {k: v for k, v in (r.get("conditions") or {}).items() if v and k != "discount"}
        if cond:
            lines.append("ℹ️ " + " · ".join(cond.values()))
        lines.append(f"⏳ Held until {_when(r['hold_until']).split(', ')[-1]}.")
        return lines, actions
    if tool == "pay_offer":
        if status == "AWAITING_APPROVAL":
            if r.get("payment_method") == "none":
                lines.append(f"✅ Confirm this free TEST reservation: {r.get('title')}?")
            else:
                method = ("Razorpay link: you pay in your own UPI app" if r.get("payment_method") == "razorpay"
                          else "TEST payment, nothing is charged")
                lines.append(f"💳 Approve {_rs(r.get('amount_minor'))} for {r.get('title')}? ({method})")
            actions.append({"type": "approval", "approval_id": s.get("approval_id"), "step_key": s["key"]})
        elif status == "SUBMITTED_EXTERNALLY" and r.get("payment_pending"):
            lines.append(f"💳 Pay {_rs(r['amount_minor'])} here (UPI: GPay / PhonePe / Paytm, or card): {r['payment_url']}")
            lines.append("I'll confirm once Razorpay confirms the payment; nothing is booked before that.")
        elif status == "CONFIRMED_BY_PROVIDER":
            icon = _ICON.get(r.get("vertical", ""), "✅")
            lines.append(f"✅ TEST booking confirmed · {r['booking_code']}")
            lines.append(f"{icon} {r['title']} · via {r['source_name']}" + (f" · {_when(r['when'])}" if r.get("when")
                                                                             and r.get("vertical") != "product" else ""))
            if r.get("amount_minor"):
                lines.append(f"💰 {_rs(r['amount_minor'])} (test payment {r['payment_ref']}; nothing charged)")
            if (r.get("conditions") or {}).get("cancellation"):
                lines.append(f"↩️ {r['conditions']['cancellation']}. Say \"cancel my booking\" to cancel.")
            lines.append(f"This is a sandbox booking: no real ticket/order. To book for real: {r.get('link')}")
        elif status == "REJECTED":
            lines.append("✋ Theek hai, booking cancel. Koi paisa nahi kata." if hl
                         else "✋ Okay, not booked. Nothing was charged.")
        elif status == "SKIPPED":
            return [], []
        else:
            return None
        return lines, actions
    if tool == "cancel_booking" and status == "COMPLETED_LOCALLY":
        if kind == "no_booking":
            lines.append("🧾 I don't see an active booking to cancel.")
        elif kind == "already_cancelled":
            lines.append(f"🧾 {r['title']} is already cancelled.")
        elif kind == "cannot_cancel":
            lines.append(f"🧾 {r['title']}: {r['why']}")
        else:
            refund = (f"Refund {_rs(r['refund_minor'])} of {_rs(r['paid_minor'])}" if r.get("paid_minor")
                      else "Nothing had been paid")
            lines.append(f"✋ TEST booking cancelled · {r['title']}" + (f" ({r['code']})" if r.get("code") else ""))
            lines.append(f"↩️ {refund} · policy: {r['policy']} (sandbox: no real money moves)")
        return lines, actions

    # ---------------------------------------------------------------- price alerts
    if tool == "watch_price" and status == "SCHEDULED":
        tgt = f" or below {_rs(r['target_minor'])}" if r.get("target_minor") else " (by 3% or more)"
        lines.append(f"🔔 Watching {r['title']}: now {_rs(r['current_minor'])} on {r['current_source']} (TEST prices). "
                     f"I'll message you when it drops{tgt}, until {_when(r['ends_at']).split(',')[0]}.")
        if r.get("already_below"):
            lines.append(f"👉 It's already at or below your target. Say \"compare {r['title']}\" to book now.")
        lines.append("Say \"my price alerts\" to see them or \"stop price alert\" to stop.")
        return lines, actions
    if tool == "watch_price" and status == "COMPLETED_LOCALLY" and kind == "no_offers":
        return ["🔎 I couldn't find that in the TEST catalogue, so there's nothing to watch."], actions
    if tool == "manage_price_watch" and status == "COMPLETED_LOCALLY":
        if kind == "watches_stopped":
            lines.append("🔕 Stopped: " + ", ".join(r["titles"]) if r.get("titles") else "🔕 No matching price alerts.")
        elif not r.get("items"):
            lines.append("🔔 You have no active price alerts.")
        else:
            lines.append("🔔 Your price alerts (TEST prices):")
            for x in r["items"]:
                tgt = f" · target {x['target']}" if x.get("target") else ""
                lines.append(f"• {x['title']} · started {x['start']} · last {x['last']}{tgt} · until {x['ends']}")
        return lines, actions

    # ---------------------------------------------------------------- shopping list
    if tool == "shopping_list" and status == "COMPLETED_LOCALLY":
        act, items = r.get("action"), r.get("items") or []
        if act == "add":
            lines.append(f"📝 Added {', '.join(r.get('changed') or [])}.")
        elif act == "remove":
            lines.append(f"📝 Removed {', '.join(r.get('changed') or [])}." if r.get("changed")
                         else "📝 Those items weren't on your list.")
        elif act == "clear":
            lines.append("📝 Cleared your shopping list.")
        if items:
            lines.append("🛒 Shopping list:\n" + "\n".join(f"• {x}" for x in items))
            lines.append("Forward this message to share it, or say \"order my list\" to compare stores and order.")
        elif act != "clear":
            lines.append("🛒 Your shopping list is empty.")
        return lines, actions

    # ---------------------------------------------------------------- web
    if tool == "web_search" and status == "COMPLETED_LOCALLY":
        if kind == "web_unavailable":
            return ["🌐 I couldn't reach the web search sources just now. Please try again in a minute."], actions
        res = r.get("results") or []
        lines.append(f"🌐 {r.get('query')} · live results ({r.get('provider')}):")
        if r.get("summary"):
            lines.append(f"Summary (by {r.get('provider')}): {r['summary']}")
        if not res:
            lines.append("No results found.")
        for x in res[:5]:
            date = f" · {x['published']}" if x.get("published") else ""
            lines.append(f"• {x['title']} ({x['source']}{date})\n  {x['url']}")
            if x.get("snippet") and x["source"] == "Wikipedia":
                lines.append(f"  {x['snippet'][:400]}")
        if r.get("ipo_official"):
            lines.append("ℹ️ Live IPO subscription, allotment and GMP figures need exchange data licensing, so I don't "
                         "show them. Official pages: " + " · ".join(f"{n}: {u}" for n, u in r["ipo_official"]))
        lines.append("Headlines are from the sources above; open the link to read the full story.")
        return lines, actions

    # ---------------------------------------------------------------- inbox / Google
    if tool == "read_inbox" and status == "COMPLETED_LOCALLY":
        test = r.get("environment") == "mock"
        lines.append("📥 Latest emails" + (" (🧪 TEST mailbox — say \"connect Google\" for your Gmail)" if test else
                                          " (Gmail)") + ":")
        if not r.get("threads"):
            lines.append("No emails found.")
        for i, t in enumerate(r.get("threads") or [], 1):
            lines.append(f"{i}. {t['from']}: {t['subject']}\n   {t['snippet'][:160]}")
        lines.append("Say \"reply to the email from <name> …\" and I'll draft it; nothing is sent without your approval.")
        return lines, actions
    if tool == "connect_google" and status == "COMPLETED_LOCALLY" and kind == "google_link":
        lines.append("🔗 Connect Google (Gmail + Calendar): sign in on Google's own page here, within "
                     f"{r.get('minutes', 10)} minutes:\n{r['url']}")
        lines.append("I'll be able to read your inbox, send emails you approve, and read/add calendar events. "
                     "Say \"disconnect Google\" any time.")
        return lines, actions
    if tool == "calendar_list" and status == "COMPLETED_LOCALLY" and kind == "calendar":
        from datetime import date as _d

        day = _d.fromisoformat(r["day"]).strftime("%a %d %b")
        lines.append(f"📅 Your calendar from {day}" + (f" ({r['days']} days)" if r.get("days", 1) > 1 else "") + ":")
        if not r.get("events"):
            lines.append("Nothing scheduled.")
        for e in r.get("events") or []:
            when = "All day" if e.get("all_day") else _when(e.get("start"))
            lines.append(f"• {when} · {e['title']}" + (f" · {e['location']}" if e.get("location") else ""))
        return lines, actions
    if tool == "calendar_create_event":
        if status == "AWAITING_APPROVAL":
            lines.append(f"📅 Add \"{r.get('title')}\" to your Google Calendar at {_when(r.get('start_local'))} "
                         f"({r.get('duration_min')} min)? No one else is invited.")
            actions.append({"type": "approval", "approval_id": s.get("approval_id"), "step_key": s["key"]})
        elif status == "CONFIRMED_BY_PROVIDER":
            lines.append(f"✅ Added to Google Calendar: {r.get('title')} · {r.get('local')}" +
                         (f"\n{r['link']}" if r.get("link") else ""))
        elif status == "REJECTED":
            lines.append("✋ Okay, not added.")
        else:
            return None
        return lines, actions
    return None
