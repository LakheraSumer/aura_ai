"""Commerce flows end to end: real Postgres + Temporal + workflows + approvals, scripted plans (no paid model),
sandbox stores, and a mocked Razorpay API (no network, no real money)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from apna.agent import planner as planner_mod
from apna.agent.schemas import DraftPlan, DraftStep
from apna.core.config import get_settings
from apna.db.models import (
    AuthChallenge,
    Message,
    PaymentIntent,
    ProviderAccount,
    SandboxGroceryOrder,
    Task,
    TaskStep,
    ToolExecution,
    User,
)
from apna.db.session import session_scope
from apna.providers import grocery, payments
from conftest import say, wait_task
from sqlalchemy import func, select

DONE = {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED", "EXPIRED"}
WAIT = {"WAITING_FOR_USER"} | DONE


def _step(task: dict, tool: str) -> dict:
    return next(s for s in task["steps"] if s["tool"] == tool)


async def _approve(api, headers, task: dict, tool: str, decision: str = "APPROVED"):
    appr = _step(task, tool)["result"]["approval_id"]
    return await api.post(f"/v1/tasks/{task['id']}/approve", headers=headers,
                          json={"approval_id": appr, "decision": decision})


async def _wait_step(api, headers, task_id: str, tool: str, statuses: set[str], timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = (await api.get(f"/v1/tasks/{task_id}", headers=headers)).json()
        if _step(t, tool)["status"] in statuses:
            return t
        await asyncio.sleep(0.3)
    raise AssertionError(f"{tool} never reached {statuses}: {json.dumps(t, default=str)[:1500]}")


def _plan(tool: str, args: dict):
    def build(inp):
        return DraftPlan(goal=inp.text, constraints=[], steps=[DraftStep(key="s1", tool=tool, description=tool,
                                                                         args=args)])
    return build


@pytest.fixture
def scripted():
    added: list = []

    def add(needle: str, tool: str, args: dict):
        planner_mod.SCRIPTED.append((needle, _plan(tool, args)))
        added.append(planner_mod.SCRIPTED[-1])
    yield add
    for x in added:
        planner_mod.SCRIPTED.remove(x)


async def _user_id(headers) -> uuid.UUID:
    import jwt

    return uuid.UUID(jwt.decode(headers["Authorization"].split()[1], options={"verify_signature": False})["sub"])


EGGS_BLINKIT = {"items": [{"name": "eggs", "qty": 1, "size": "12"}], "store": "Blinkit", "city": "Pune",
                "area": "Kothrud"}


# --------------------------------------------------------------------------- acceptance example


async def test_acceptance_blinkit_or_zepto_cheapest_total(api, user, scripted):
    first = {"items": [{"name": "milk", "qty": 1, "size": "2 litre"}, {"name": "eggs", "qty": 1, "size": "12"}],
             "stores": ["Blinkit", "Zepto"], "choose": "cheapest", "city": "Pune", "area": "Kothrud"}
    scripted("jo total mein sasta ho", "order_groceries", first)
    scripted("Amul Taaza Toned Milk milk", "order_groceries",
             {**first, "items": [{"name": "milk", "qty": 1, "size": "2 litre", "brand": "Amul Taaza Toned Milk"},
                                 {"name": "eggs", "qty": 1, "size": "12"}]})
    tid = await say(api, user, "Blinkit ya Zepto se 2 litre milk aur 12 eggs mangwa do, jo total mein sasta ho. "
                               "Kothrud, Pune")
    task = await wait_task(api, user, tid, WAIT)
    q = _step(task, "order_groceries")
    assert q["status"] == "AWAITING_USER_ACTION" and q["result"]["question_key"] == "variant_choice"  # asked, not guessed
    assert q["result"]["choices"][0].startswith("Amul Taaza Toned Milk")

    tid2 = await say(api, user, "1")
    task = await wait_task(api, user, tid2, WAIT)
    cart = _step(task, "order_groceries")["result"]
    assert cart["kind"] == "cart" and len(cart["compared"]) == 2  # both apps priced with fees
    assert cart["store"] in ("Blinkit", "Zepto") and cart["quote_expires_at"] and "TEST" in cart["payment_method"]
    milk = [x for x in cart["lines"] if "Milk" in x["name"]]
    assert sum(_ml(x["size"]) * x["qty"] for x in milk) == 2000  # equivalent quantity, any pack mix
    pay = _step(task, "pay_grocery_order")
    assert pay["status"] == "AWAITING_APPROVAL" and pay["result"]["amount_minor"] == cart["total_minor"]
    async with session_scope() as s:  # nothing placed before approval
        assert (await s.execute(select(SandboxGroceryOrder.status).where(
            SandboxGroceryOrder.id == uuid.UUID(cart["order_id"])))).scalar_one() == "CART"

    assert (await _approve(api, user, task, "pay_grocery_order")).status_code == 202
    task = await _wait_step(api, user, tid2, "pay_grocery_order", {"CONFIRMED_BY_PROVIDER", "FAILED"})
    done = _step(task, "pay_grocery_order")["result"]
    assert done["order_code"].startswith("TEST-ORD-") and done["environment"] == "sandbox"
    assert (await _approve(api, user, task, "pay_grocery_order")).status_code == 409  # approval is single-use
    async with session_scope() as s:
        o = await s.get(SandboxGroceryOrder, uuid.UUID(cart["order_id"]))
        assert o.status == "PLACED" and o.order_code == done["order_code"]


def _ml(size: str) -> float:
    n, unit = size.split()
    return float(n) * (1000 if unit == "l" else 1)


# --------------------------------------------------------------------------- real gateway path (mocked API)


class FakeRazorpay:
    def __init__(self) -> None:
        self.links: dict[str, str] = {}
        self.created = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v1/payment_links":
            self.created += 1
            lid = f"plink_{self.created}_{uuid.uuid4().hex[:6]}"
            self.links[lid] = "created"
            return httpx.Response(200, json={"id": lid, "short_url": f"https://rzp.io/i/{lid}", "status": "created"})
        lid = req.url.path.rsplit("/", 1)[-1]
        st = self.links.get(lid, "created")
        pays = [{"payment_id": "pay_test_1", "status": "captured"}] if st == "paid" else []
        return httpx.Response(200, json={"id": lid, "status": st, "payments": pays})


@pytest.fixture
def razorpay(monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_dummy")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "test_secret")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "whsec_test")
    get_settings.cache_clear()
    monkeypatch.setattr(payments, "TRANSPORT", httpx.MockTransport(fake.handler))
    yield fake
    monkeypatch.undo()
    get_settings.cache_clear()


def _rzp_post(api, body: dict, event_id: str, secret: str = "whsec_test"):
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return api.post("/v1/webhooks/razorpay", content=raw, headers={
        "x-razorpay-signature": sig, "x-razorpay-event-id": event_id, "content-type": "application/json"})


async def _gateway_cart(api, user, scripted, needle: str) -> tuple[str, dict]:
    scripted(needle, "order_groceries", EGGS_BLINKIT)
    tid = await say(api, user, needle)
    task = await wait_task(api, user, tid, WAIT)
    assert _step(task, "pay_grocery_order")["result"].get("payment_method") == "razorpay"
    return tid, task


async def test_payment_link_webhook_places_order_once(api, user, scripted, razorpay):
    tid, task = await _gateway_cart(api, user, scripted, "eggs via razorpay one")
    assert (await _approve(api, user, task, "pay_grocery_order")).status_code == 202
    task = await _wait_step(api, user, tid, "pay_grocery_order", {"SUBMITTED_EXTERNALLY", "FAILED"})
    pending = _step(task, "pay_grocery_order")["result"]
    assert pending["payment_pending"] and pending["payment_url"].startswith("https://rzp.io/")
    order_id = uuid.UUID(_step(task, "order_groceries")["result"]["order_id"])
    async with session_scope() as s:  # a payment link is not proof of payment
        assert (await s.get(SandboxGroceryOrder, order_id)).status == "PAYMENT_PENDING"
        intent = (await s.execute(select(PaymentIntent).where(PaymentIntent.purpose_ref == str(order_id)))).scalar_one()
    link_id = intent.gateway_ref
    body = {"event": "payment_link.paid", "payload": {"payment_link": {"entity": {"id": link_id, "status": "paid"}},
                                                      "payment": {"entity": {"id": "pay_test_1"}}}}
    assert (await _rzp_post(api, body, "evt_bad", secret="wrong")).status_code == 403  # forged event refused
    razorpay.links[link_id] = "paid"
    r = await _rzp_post(api, body, f"evt_{link_id}")
    assert r.status_code == 200 and r.json()["matched"]
    assert (await _rzp_post(api, body, f"evt_{link_id}")).json() == {"duplicate": True}  # replayed delivery
    await _until(lambda o: o.status == "PLACED", order_id)
    async with session_scope() as s:
        o = await s.get(SandboxGroceryOrder, order_id)
        assert o.payment_ref.startswith("razorpay:") and o.order_code.startswith("TEST-ORD-")
        assert (await s.get(PaymentIntent, intent.id)).status == "PAID"
        notes = (await s.execute(select(func.count()).select_from(Message).where(
            Message.dedupe_key == f"payment-done:{intent.id}"))).scalar_one()
        assert notes == 1  # one confirmation, even with a duplicate webhook + polling
    assert razorpay.created == 1  # exactly one payment link


async def test_unpaid_link_expires_and_nothing_is_ordered(api, user, scripted, razorpay):
    tid, task = await _gateway_cart(api, user, scripted, "eggs via razorpay expiring")
    await _approve(api, user, task, "pay_grocery_order")
    task = await _wait_step(api, user, tid, "pay_grocery_order", {"SUBMITTED_EXTERNALLY"})
    order_id = uuid.UUID(_step(task, "order_groceries")["result"]["order_id"])
    async with session_scope() as s:
        intent = (await s.execute(select(PaymentIntent).where(PaymentIntent.purpose_ref == str(order_id)))).scalar_one()
    razorpay.links[intent.gateway_ref] = "expired"  # no webhook at all: reconciliation must find out
    await _until(lambda o: o.status == "CANCELLED", order_id)
    async with session_scope() as s:
        assert (await s.get(PaymentIntent, intent.id)).status == "EXPIRED"
        msg = (await s.execute(select(Message.content).where(Message.dedupe_key == f"payment-done:{intent.id}"))).scalar_one()
        assert "not completed" in msg and "Nothing was ordered" in msg


async def _until(pred, order_id: uuid.UUID, timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with session_scope() as s:
            if pred(await s.get(SandboxGroceryOrder, order_id)):
                return
        await asyncio.sleep(0.5)
    raise AssertionError("order never reached the expected state")


# --------------------------------------------------------------------------- price change, stale carts, crashes


async def test_changed_price_after_quote_expiry_needs_new_approval(api, user, scripted, monkeypatch):
    scripted("eggs price check", "order_groceries", EGGS_BLINKIT)
    tid = await say(api, user, "eggs price check")
    task = await wait_task(api, user, tid, WAIT)
    order_id = uuid.UUID(_step(task, "order_groceries")["result"]["order_id"])
    async with session_scope() as s:
        o = await s.get(SandboxGroceryOrder, order_id)
        o.quote_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    monkeypatch.setitem(grocery.STORES, "blinkit", grocery.Store(**{**grocery.STORES["blinkit"].__dict__,
                                                                    "price_factor": 1.10}))
    await _approve(api, user, task, "pay_grocery_order")
    task = await _wait_step(api, user, tid, "pay_grocery_order", {"AWAITING_USER_ACTION", "CONFIRMED_BY_PROVIDER"})
    q = _step(task, "pay_grocery_order")["result"]
    assert q["question_key"] == "price_changed" and q["old_total"] != q["new_total"]
    async with session_scope() as s:
        assert (await s.get(SandboxGroceryOrder, order_id)).status == "CANCELLED"  # old approval can't charge


async def test_old_pay_button_after_cart_edit_charges_nothing(api, user, scripted):
    scripted("milk and bread for edit test", "order_groceries",
             {"items": [{"name": "milk", "qty": 2, "brand": "Amul Taaza Toned Milk"},
                        {"name": "bread", "qty": 1, "brand": "Britannia White Bread"}],
              "store": "Blinkit", "city": "Pune", "area": "Kothrud"})
    scripted("remove the bread please", "update_grocery_cart", {"changes": [{"action": "remove", "name": "bread"}]})
    old = await wait_task(api, user, await say(api, user, "milk and bread for edit test"), WAIT)
    new = await wait_task(api, user, await say(api, user, "remove the bread please"), WAIT)
    assert [x["name"] for x in _step(new, "update_grocery_cart")["result"]["lines"]] == ["Amul Taaza Toned Milk"]
    await _approve(api, user, old, "pay_grocery_order")
    old = await _wait_step(api, user, old["id"], "pay_grocery_order", {"FAILED", "CONFIRMED_BY_PROVIDER"})
    assert _step(old, "pay_grocery_order")["status"] == "FAILED"
    assert "replaced" in _step(old, "pay_grocery_order")["error"]
    await _approve(api, user, new, "pay_grocery_order")
    new = await _wait_step(api, user, new["id"], "pay_grocery_order", {"CONFIRMED_BY_PROVIDER", "FAILED"})
    assert _step(new, "pay_grocery_order")["status"] == "CONFIRMED_BY_PROVIDER"


async def test_interrupted_payment_is_reconciled_not_repeated(api, user, scripted):
    scripted("eggs crash test", "order_groceries", EGGS_BLINKIT)
    tid = await say(api, user, "eggs crash test")
    task = await wait_task(api, user, tid, WAIT)
    pay = _step(task, "pay_grocery_order")
    async with session_scope() as s:  # a previous attempt started and the worker died mid-flight
        step = (await s.execute(select(TaskStep).where(TaskStep.task_id == uuid.UUID(tid),
                                                       TaskStep.step_key == pay["key"]))).scalar_one()
        t = await s.get(Task, uuid.UUID(tid))
        s.add(ToolExecution(user_id=t.user_id, task_id=t.id, step_id=step.id, idempotency_key=f"{tid}:{pay['key']}",
                            tool="pay_grocery_order", status="STARTED", result={}))
    await _approve(api, user, task, "pay_grocery_order")
    task = await _wait_step(api, user, tid, "pay_grocery_order", {"AWAITING_USER_ACTION", "CONFIRMED_BY_PROVIDER"})
    assert _step(task, "pay_grocery_order")["result"].get("outcome_uncertain") is True
    order_id = uuid.UUID(_step(task, "order_groceries")["result"]["order_id"])
    async with session_scope() as s:
        assert (await s.get(SandboxGroceryOrder, order_id)).status == "CART"  # not placed blindly


# --------------------------------------------------------------------------- isolation, OTP page, redaction


async def test_users_and_providers_are_isolated(api, user, scripted):
    from apna.security import crypto
    from apna.services import accounts

    a_id = await _user_id(user)
    r = await api.post("/v1/auth/register", json={"email": f"b-{uuid.uuid4().hex[:6]}@example.test",
                                                  "password": "correct horse battery", "display_name": "B"})
    b_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    b_id = await _user_id(b_headers)
    async with session_scope() as s:
        await accounts.link_account(s, a_id, "zomato", "token-of-A")
    async with session_scope() as s:
        assert await accounts.get_token(s, a_id, "zomato") == "token-of-A"
        assert await accounts.get_token(s, b_id, "zomato") is None
        row = (await s.execute(select(ProviderAccount).where(ProviderAccount.user_id == a_id))).scalar_one()
        with pytest.raises(ValueError):  # A's ciphertext copied to B can't be decrypted as B's
            crypto.decrypt(row.token_ciphertext, f"{b_id}:zomato")
        assert "token-of-A" not in row.token_ciphertext
        assert await accounts.revoke(s, a_id, "zomato")
    async with session_scope() as s:
        assert await accounts.get_token(s, a_id, "zomato") is None
    scripted("what happened to my order isolation", "get_order_status", {})
    t = await wait_task(api, b_headers, await say(api, b_headers, "what happened to my order isolation"), WAIT)
    assert _step(t, "get_order_status")["result"]["items"] == []  # B sees none of A's orders


async def test_otp_secure_page_flow(api, user):
    from apna.core.errors import Conflict
    from apna.security import otp

    uid = await _user_id(user)
    async with session_scope() as s:
        ch, token = await otp.create_challenge(s, user_id=uid, provider="Swiggy", purpose="log in to Swiggy",
                                               session_ref="sess-1")
        ch_id = ch.id
    page = await api.get(f"/v1/secure/challenge/{token}")
    assert page.status_code == 200 and "Swiggy" in page.text and "log in to Swiggy" in page.text
    assert (await api.post(f"/v1/secure/challenge/{token}", data={"code": "12ab"})).status_code == 422
    assert (await api.post(f"/v1/secure/challenge/{token}", data={"code": "482913"})).status_code == 200
    assert (await api.post(f"/v1/secure/challenge/{token}", data={"code": "482913"})).status_code == 409  # one use
    async with session_scope() as s:
        row = await s.get(AuthChallenge, ch_id)
        assert row.attempts == 2 and "482913" not in (row.secret_ciphertext or "")
        assert await otp.consume_code(s, ch_id, user_id=uid, provider="Swiggy") == "482913"
    async with session_scope() as s:
        assert (await s.get(AuthChallenge, ch_id)).secret_ciphertext is None
        with pytest.raises(Conflict):
            await otp.consume_code(s, ch_id, user_id=uid, provider="Swiggy")  # wiped after one use
        stored = (await s.execute(select(func.count()).select_from(Message).where(
            Message.content.contains("482913")))).scalar_one()
        assert stored == 0  # never in the chat transcript
        ch2, token2 = await otp.create_challenge(s, user_id=uid, provider="Swiggy", purpose="log in", session_ref="x")
        ch2.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await api.get(f"/v1/secure/challenge/{token2}")).status_code == 409  # expired


async def test_secrets_typed_in_chat_are_masked_and_not_planned(api, user):
    r = await api.post("/v1/chat/messages", json={"text": "my otp is 482913"}, headers=user)
    assert r.status_code == 202 and r.json().get("redacted") == ["otp"] and r.json().get("task_id") is None
    async with session_scope() as s:
        uid = await _user_id(user)
        rows = (await s.execute(select(Message.content).where(Message.user_id == uid))).scalars().all()
        assert not any("482913" in c for c in rows) and any("removed" in c for c in rows)
        assert (await s.execute(select(func.count()).select_from(User).where(User.id == uid))).scalar_one() == 1
