"""Comparison -> pick -> hold -> approval -> TEST booking, cancellations, price-drop alerts, shopping lists,
reminder follow-ups, web search and Google (Gmail + Calendar) end to end. Real Postgres + Temporal; every
outside HTTP API is a MockTransport (no network, no real money, no real email)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from apna.core.config import get_settings
from apna.core.errors import NotFound
from apna.db.models import (
    Message,
    PriceWatch,
    ProviderAccount,
    Reminder,
    SandboxBooking,
    ShoppingListItem,
)
from apna.db.session import session_scope
from apna.providers import google, websearch
from apna.workflows.definitions import DEFAULT_QUEUE, ReminderWorkflow
from conftest import say, wait_task
from sqlalchemy import select
from test_commerce import WAIT, _approve, _step, _user_id, _wait_step, scripted  # noqa: F401 - fixture re-export


async def _search_then_pick(api, user, scripted, needle, args, pick="1"):
    scripted(needle, "search_offers", args)
    tid = await say(api, user, needle)
    task = await wait_task(api, user, tid, WAIT)
    q = _step(task, "search_offers")
    assert q["status"] == "AWAITING_USER_ACTION" and q["result"]["question_key"] == "offer_choice", q
    tid2 = await say(api, user, pick)
    task = await wait_task(api, user, tid2, WAIT)
    return q["result"], tid2, task


async def test_compare_product_pick_cheapest_approve_and_cancel(api, user, scripted):
    r, tid, task = await _search_then_pick(api, user, scripted, "iphone 16 sabse saste mein",
                                           {"vertical": "product", "query": "iPhone 16"}, pick="sabse sasta")
    assert len(r["sources"]) >= 2 and r["choices"][0]  # same phone priced at several stores
    held = _step(task, "book_offer")["result"]
    assert held["kind"] == "held" and held["title"] == "Apple iPhone 16 (128 GB)"
    pay = _step(task, "pay_offer")
    assert pay["status"] == "AWAITING_APPROVAL" and pay["result"]["amount_minor"] == held["amount_minor"]
    async with session_scope() as s:  # nothing booked before approval
        assert (await s.get(SandboxBooking, uuid.UUID(held["booking_id"]))).status == "HELD"

    assert (await _approve(api, user, task, "pay_offer")).status_code == 202
    task = await _wait_step(api, user, tid, "pay_offer", {"CONFIRMED_BY_PROVIDER", "FAILED"})
    done = _step(task, "pay_offer")["result"]
    assert done["booking_code"].startswith("TEST-PRO-") and done["environment"] == "sandbox"
    assert (await _approve(api, user, task, "pay_offer")).status_code == 409  # single-use approval

    scripted("what happened to my booking", "get_order_status", {})
    st = await wait_task(api, user, await say(api, user, "what happened to my booking"), WAIT)
    assert any(x["ref"] == done["booking_code"] for x in _step(st, "get_order_status")["result"]["items"])

    scripted("cancel my booking please", "cancel_booking", {})
    c = await wait_task(api, user, await say(api, user, "cancel my booking please"), WAIT)
    res = _step(c, "cancel_booking")["result"]
    assert res["kind"] == "booking_cancelled" and res["refund_minor"] == held["amount_minor"]


async def test_free_table_reservation_needs_confirmation_but_no_payment(api, user, scripted):
    r, tid, task = await _search_then_pick(api, user, scripted, "dinner table for 4 at 8 pm",
                                           {"vertical": "table", "query": "dinner", "city": "Pune",
                                            "time_pref": "8 pm", "qty": 4})
    pay = _step(task, "pay_offer")
    assert pay["status"] == "AWAITING_APPROVAL" and pay["result"]["payment_method"] == "none"
    await _approve(api, user, task, "pay_offer")
    task = await _wait_step(api, user, tid, "pay_offer", {"CONFIRMED_BY_PROVIDER", "FAILED"})
    assert _step(task, "pay_offer")["result"]["booking_code"].startswith("TEST-TAB-")


async def test_unknown_offer_books_nothing_and_skips_payment(api, user):
    task = await wait_task(api, user, await say(api, user, "book offer OFR-DEADBEEF"), WAIT)
    assert _step(task, "book_offer")["result"]["kind"] == "offer_gone"
    assert _step(task, "pay_offer")["status"] == "SKIPPED" and task["status"] in ("COMPLETED", "PARTIALLY_COMPLETED")


async def test_unrelated_reply_to_a_comparison_is_a_new_request(api, user, scripted):
    scripted("flights pune to delhi", "search_offers", {"vertical": "flight", "origin": "Pune",
                                                        "destination": "Delhi", "when_text": "kal"})
    scripted("weather in goa", "get_weather", {"place": "Goa"})
    await wait_task(api, user, await say(api, user, "flights pune to delhi kal"), WAIT)
    from apna.db.models import Task

    tid = await say(api, user, "what's the weather in goa")
    async with session_scope() as s:
        goal = (await s.get(Task, uuid.UUID(tid))).goal
    assert goal == "what's the weather in goa"  # not glued to the flight search, not a booking


async def test_price_drop_alert_messages_the_user(api, user, scripted):
    scripted("alert me when kindle", "watch_price", {"vertical": "product", "query": "kindle",
                                                     "target_price": 9_000_000})
    task = await wait_task(api, user, await say(api, user, "alert me when kindle gets cheaper"), WAIT)
    w = _step(task, "watch_price")
    assert w["status"] == "SCHEDULED" and w["result"]["already_below"]
    wid = uuid.UUID(w["result"]["watch_id"])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        async with session_scope() as s:
            watch = await s.get(PriceWatch, wid)
            msg = (await s.execute(select(Message).where(Message.dedupe_key == f"pricewatch:{wid}:TRIGGERED"))
                   ).scalar_one_or_none()
        if watch.status == "TRIGGERED" and msg is not None:
            assert "Price drop" in msg.content and "TEST" in msg.content
            return
        await asyncio.sleep(0.5)
    raise AssertionError("price watch never triggered")


async def test_shopping_list_add_show_and_order(api, user, scripted):
    scripted("add milk and eggs to my list", "shopping_list",
             {"action": "add", "items": [{"name": "milk", "qty": 1, "size": "1 litre",
                                          "brand": "Amul Taaza Toned Milk"},  # named: no "which milk?" question
                                         {"name": "eggs", "qty": 1, "size": "6"}]})
    t = await wait_task(api, user, await say(api, user, "add milk and eggs to my list"), WAIT)
    assert _step(t, "shopping_list")["result"]["items"] == ["Amul Taaza Toned Milk milk 1 litre", "eggs 6"]
    scripted("order my list to kothrud", "order_shopping_list", {"city": "Pune", "area": "Kothrud",
                                                                  "choose": "cheapest"})
    t = await wait_task(api, user, await say(api, user, "order my list to kothrud"), WAIT)
    cart = _step(t, "order_shopping_list")["result"]
    assert cart["kind"] == "cart" and _step(t, "pay_grocery_order")["status"] == "AWAITING_APPROVAL"
    async with session_scope() as s:
        uid = await _user_id(user)
        assert len((await s.execute(select(ShoppingListItem).where(ShoppingListItem.user_id == uid))).all()) == 2


async def test_reminder_follow_up_done_and_snooze(api, user, temporal, scripted):
    from apna.services.conversation import ConversationService

    scripted("hello there", "answer_question", {"answer": "Hi!"})
    await wait_task(api, user, await say(api, user, "hello there"), WAIT)  # creates the conversation
    uid = await _user_id(user)
    rid = uuid.uuid4()
    async with session_scope() as s:
        s.add(Reminder(id=rid, user_id=uid, text="take medicine", fire_at_utc=datetime.now(UTC), timezone="Asia/Kolkata",
                       local_display="now", idempotency_key=f"it-{rid}", status="SCHEDULED",
                       workflow_id=f"reminder-{rid}"))
    await temporal.start_workflow(ReminderWorkflow.run, args=[str(rid), datetime.now(UTC).isoformat(), 1],
                                  id=f"reminder-{rid}", task_queue=DEFAULT_QUEUE)
    deadline = time.monotonic() + 20
    nudge = None
    while time.monotonic() < deadline and nudge is None:
        async with session_scope() as s:
            nudge = (await s.execute(select(Message).where(Message.dedupe_key == f"reminder-followup:{rid}:0"))
                     ).scalar_one_or_none()
        await asyncio.sleep(0.5)
    assert nudge is not None and "take medicine" in nudge.content
    assert nudge.payload["actions"][0]["buttons"][0][0] == f"rdone:{rid}"  # Done / Snooze buttons

    conv = ConversationService(temporal)
    async with session_scope() as s:
        out = await conv.reminder_button(s, uid, "rsnooze", rid)
    assert out["kind"] == "reminder_snoozed"
    async with session_scope() as s:
        r = await s.get(Reminder, rid)
        assert r.snoozes == 1 and r.status == "SCHEDULED" and r.fire_at_utc > datetime.now(UTC) + timedelta(minutes=9)
        await conv.reminder_button(s, uid, "rdone", rid)
    async with session_scope() as s:
        assert (await s.get(Reminder, rid)).done_at is not None
    with pytest.raises(NotFound):  # another user's reminder is never touched
        async with session_scope() as s:
            await conv.reminder_button(s, uuid.uuid4(), "rdone", rid)


RSS = (b"<?xml version='1.0'?><rss><channel><item><title>Sensex ends higher - Example News</title>"
       b"<link>https://news.example.test/s</link><pubDate>Sun, 16 Nov 2026 06:00:00 GMT</pubDate>"
       b"<source url='https://news.example.test'>Example News</source></item></channel></rss>")


async def test_web_search_returns_sources_with_links(api, user, scripted, monkeypatch):
    monkeypatch.setattr(websearch, "TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(200, content=RSS)))
    scripted("sensex news today", "web_search", {"query": "sensex today", "kind": "news"})
    t = await wait_task(api, user, await say(api, user, "sensex news today"), WAIT)
    r = _step(t, "web_search")["result"]
    assert r["provider"] == "Google News" and r["results"][0]["url"] == "https://news.example.test/s"


# --------------------------------------------------------------------------- Google (mocked Google APIs)


class FakeGoogle:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.token_calls: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(req.content.decode())
            self.token_calls.append(form)
            if form["grant_type"] == ["authorization_code"]:
                assert form["code_verifier"][0] and form["code"] == ["good-code"]
                return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-secret-1",
                                                 "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600})
        assert req.headers["authorization"].startswith("Bearer at-")
        if "/gmail/v1/users/me/messages/send" in url:
            self.sent.append({"url": url})
            return httpx.Response(200, json={"id": "sent-1"})
        if "/gmail/v1/users/me/messages/m1" in url:
            return httpx.Response(200, json={"id": "m1", "threadId": "t1", "snippet": "Lunch tomorrow?",
                                             "internalDate": "1763270000000", "payload": {"headers": [
                                                 {"name": "From", "value": "Priya <priya@example.test>"},
                                                 {"name": "Subject", "value": "Lunch"}]}})
        if "/gmail/v1/users/me/messages" in url:
            return httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}]})
        if "/calendar/v3/calendars/primary/events" in url and req.method == "POST":
            body = json.loads(req.content)
            if body["id"] in self.events:
                return httpx.Response(409, json={"error": "duplicate"})
            self.events[body["id"]] = body
            return httpx.Response(200, json={"id": body["id"], "htmlLink": "https://calendar.google.com/e",
                                             "summary": body["summary"], "start": body["start"]})
        if "/calendar/v3/calendars/primary/events" in url:
            return httpx.Response(200, json={"items": [{"summary": "Standup", "start": {"dateTime":
                                                        "2026-11-17T09:30:00+05:30"}, "end": {}}]})
        return httpx.Response(404)


@pytest.fixture
def fake_google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://apna.test")
    get_settings.cache_clear()
    monkeypatch.setattr(google, "TRANSPORT", httpx.MockTransport(fake.handler))
    yield fake
    monkeypatch.undo()
    get_settings.cache_clear()


async def test_google_connect_inbox_and_calendar_with_approval(api, user, scripted, fake_google):
    scripted("connect my google", "connect_google", {})
    t = await wait_task(api, user, await say(api, user, "connect my google"), WAIT)
    link = _step(t, "connect_google")["result"]["url"]
    q = parse_qs(urlparse(link).query)
    assert link.startswith("https://accounts.google.com/") and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["https://apna.test/v1/oauth/google/callback"]
    state = q["state"][0]

    r = await api.get("/v1/oauth/google/callback", params={"state": state, "code": "good-code"})
    assert r.status_code == 200 and "connected" in r.text
    assert (await api.get("/v1/oauth/google/callback", params={"state": state, "code": "good-code"})
            ).status_code == 400  # state is single-use
    uid = await _user_id(user)
    async with session_scope() as s:
        acct = (await s.execute(select(ProviderAccount).where(ProviderAccount.user_id == uid,
                                                              ProviderAccount.provider == "google"))).scalar_one()
        assert acct.status == "LINKED" and "rt-secret-1" not in acct.token_ciphertext  # encrypted at rest

    scripted("show my latest emails", "read_inbox", {"query": ""})
    t = await wait_task(api, user, await say(api, user, "show my latest emails"), WAIT)
    inbox = _step(t, "read_inbox")["result"]
    assert inbox["provider"] == "gmail" and inbox["threads"][0]["address"] == "priya@example.test"

    scripted("add dentist to my calendar", "calendar_create_event", {"title": "Dentist",
                                                                      "when_text": "tomorrow 5 pm"})
    tid = await say(api, user, "add dentist to my calendar")
    t = await wait_task(api, user, tid, WAIT)
    ev = _step(t, "calendar_create_event")
    assert ev["status"] == "AWAITING_APPROVAL" and not fake_google.events  # nothing written before approval
    await _approve(api, user, t, "calendar_create_event")
    t = await _wait_step(api, user, tid, "calendar_create_event", {"CONFIRMED_BY_PROVIDER", "FAILED"})
    assert _step(t, "calendar_create_event")["result"]["event_id"] and len(fake_google.events) == 1
    body = next(iter(fake_google.events.values()))
    assert body["summary"] == "Dentist" and "attendees" not in body

    scripted("disconnect google", "disconnect_google", {})
    t = await wait_task(api, user, await say(api, user, "disconnect google"), WAIT)
    assert _step(t, "disconnect_google")["result"]["kind"] == "google_disconnected"
