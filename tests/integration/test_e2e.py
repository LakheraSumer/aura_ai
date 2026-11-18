"""End-to-end acceptance scenarios (spec §30) against real Postgres + Temporal + Chromium.

Numbering follows the spec's TEST n. Channel HTTP is recorded, never sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apna.agent.language import resolve_date
from apna.db.models import (
    Approval,
    BrowserAction,
    EmailMessage,
    LinkedChannel,
    Notification,
    OutboundMessage,
    Reminder,
    Task,
    ToolExecution,
)
from apna.db.session import session_scope
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


# --------------------------------------------------------------------------- TEST 1


async def test_01_reminder_tomorrow_8am_is_durably_scheduled(api, user, temporal):
    tid = await say(api, user, "Remind me tomorrow at 8 AM to call the bank")
    task = await wait_task(api, user, tid, DONE)
    assert task["status"] == "COMPLETED", task
    step = _step(task, "create_reminder")
    assert step["status"] == "SCHEDULED"
    ist = ZoneInfo("Asia/Kolkata")
    expected = datetime.combine(datetime.now(ist).date() + timedelta(days=1), datetime.min.time(), ist).replace(hour=8)
    assert step["result"]["fire_at_utc"] == expected.astimezone(UTC).isoformat()
    desc = await temporal.get_workflow_handle(step["result"]["workflow_id"]).describe()
    assert desc.status.name == "RUNNING"  # verified with the scheduler, not assumed


async def test_01b_reminder_timer_fires_once(api, user, temporal):
    """Durable timer delivers exactly one notification (idempotent delivery)."""
    from apna.workflows.definitions import ReminderWorkflow

    tid = await say(api, user, "Remind me tomorrow at 9 AM to pay rent")
    task = await wait_task(api, user, tid, DONE)
    rid = _step(task, "create_reminder")["result"]["reminder_id"]
    # Start a second, short timer for the same reminder row to observe delivery without waiting a day.
    fire = datetime.now(UTC) + timedelta(seconds=2)
    h = await temporal.start_workflow(ReminderWorkflow.run, args=[rid, fire.isoformat()],
                                      id=f"reminder-fast-{rid}", task_queue="apna-tasks")
    assert await asyncio.wait_for(h.result(), 30) == "DELIVERED"
    h2 = await temporal.start_workflow(ReminderWorkflow.run, args=[rid, fire.isoformat()],
                                       id=f"reminder-fast2-{rid}", task_queue="apna-tasks")
    await asyncio.wait_for(h2.result(), 30)
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(Notification).where(
            Notification.dedupe_key == f"reminder:{rid}"))).scalar_one()
        r = (await s.execute(select(Reminder).where(Reminder.id == uuid.UUID(rid)))).scalar_one()
    assert n == 1 and r.status == "DELIVERED"


# --------------------------------------------------------------------------- TEST 2 + 3


async def test_02_03_draft_then_approved_send(api, user):
    tid = await say(api, user, "Draft a response to my latest meeting email")
    task = await wait_task(api, user, tid, WAIT)
    assert task["status"] == "WAITING_FOR_USER"
    draft = _step(task, "draft_email")
    assert draft["status"] == "COMPLETED_LOCALLY"
    assert draft["result"]["to_address"] == "priya.manager@company.test"
    assert draft["result"]["subject"].startswith("Re: Project sync meeting")
    assert _step(task, "send_email")["status"] == "AWAITING_APPROVAL"
    async with session_scope() as s:  # TEST 2: nothing sent without approval
        assert (await s.execute(select(func.count()).select_from(EmailMessage).where(
            EmailMessage.task_id == uuid.UUID(tid), EmailMessage.status == "SENT"))).scalar_one() == 0

    r = await _approve(api, user, task, "send_email")
    assert r.status_code == 202, r.text
    task = await wait_task(api, user, tid, DONE)
    send = _step(task, "send_email")
    assert task["status"] == "COMPLETED" and send["status"] == "CONFIRMED_BY_PROVIDER"
    assert send["result"]["environment"] == "mock" and send["result"]["provider_message_id"].startswith("sandbox-")
    # Replayed approval is refused.
    assert (await _approve(api, user, task, "send_email")).status_code == 409
    msgs = (await api.get(f"/v1/conversations/{(await api.get('/v1/conversations', headers=user)).json()[0]['id']}"
                          "/messages", headers=user)).json()
    assert any("mock mailbox" in m["content"] for m in msgs if m["role"] == "assistant")


async def test_03b_changed_draft_is_not_sent(api, user):
    tid = await say(api, user, "manager ko leave ka mail draft kar")
    task = await wait_task(api, user, tid, WAIT)
    draft_id = _step(task, "draft_email")["result"]["draft_id"]
    async with session_scope() as s:  # someone edits the draft after the approval card was shown
        row = (await s.execute(select(EmailMessage).where(EmailMessage.id == uuid.UUID(draft_id)))).scalar_one()
        row.body = row.body + "\nP.S. tampered"
    await _approve(api, user, task, "send_email")
    for _ in range(100):
        task = (await api.get(f"/v1/tasks/{tid}", headers=user)).json()
        # Step and task projections are written by consecutive activities: wait for both.
        if _step(task, "send_email")["status"] != "AWAITING_APPROVAL" and task["status"] == "WAITING_FOR_USER":
            break
        await asyncio.sleep(0.3)
    send = _step(task, "send_email")
    assert send["status"] == "AWAITING_USER_ACTION" and send["result"]["question_key"] == "draft_changed"
    assert task["status"] == "WAITING_FOR_USER"
    async with session_scope() as s:
        row = (await s.execute(select(EmailMessage).where(EmailMessage.id == uuid.UUID(draft_id)))).scalar_one()
    assert row.status == "DRAFT"


async def test_03c_rejection_keeps_draft(api, user):
    tid = await say(api, user, "Draft a reply to my latest recruiter email")
    task = await wait_task(api, user, tid, WAIT)
    await _approve(api, user, task, "send_email", "REJECTED")
    task = await wait_task(api, user, tid, DONE)
    assert task["status"] == "COMPLETED" and _step(task, "send_email")["status"] == "REJECTED"


# --------------------------------------------------------------------------- TEST 4


async def test_04_train_search_never_fabricates(api, user):
    text = "Find an AC train from Pune to Mumbai next Friday"
    tid = await say(api, user, text)
    task = await wait_task(api, user, tid, WAIT)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if resolve_date(text, today).value is None:  # "next Friday" ambiguous this week: must ask, not guess
        ask = _step(task, "ask_user")
        assert ask["status"] == "AWAITING_USER_ACTION" and len(ask["result"]["options"]) == 2
        tid = await say(api, user, ask["result"]["options"][1])
        old = await wait_task(api, user, task["id"], DONE)
        assert _step(old, "ask_user")["status"] == "SKIPPED"
        task = await wait_task(api, user, tid, DONE)
    st = _step(task, "search_trains")
    assert st["status"] == "COMPLETED_LOCALLY"
    r = st["result"]
    assert r["data_available"] is False and r["trains"] == []
    assert r["official_link"] == "https://www.irctc.co.in/nget/train-search"
    assert {x["code"] for x in r["destination_stations"]} >= {"CSMT", "LTT"} and r["travel_class"] == "AC"


# --------------------------------------------------------------------------- TEST 6


async def test_06_browser_form_with_approval_and_verification(api, user):
    tid = await say(api, user, "Open the approved test website, complete the sample form and return its confirmation")
    task = await wait_task(api, user, tid, WAIT)
    step = _step(task, "browser_submit_form")
    assert step["status"] == "AWAITING_APPROVAL" and "Asha Patil" in step["result"]["fields_preview"]
    await _approve(api, user, task, "browser_submit_form")
    task = await wait_task(api, user, tid, DONE, timeout=90)
    step = _step(task, "browser_submit_form")
    assert step["status"] == "CONFIRMED_BY_PROVIDER", step
    assert step["result"]["confirmation_code"].startswith("APN-") and step["result"]["verified_with_site"] is True
    async with session_scope() as s:
        actions = (await s.execute(select(BrowserAction).where(
            BrowserAction.session_id == uuid.UUID(step["result"]["browser_session_id"])))).scalars().all()
    shots = [a.screenshot_ref for a in actions if a.screenshot_ref]
    assert {a.action for a in actions} >= {"open_url", "type_text", "click", "read_confirmation"} and shots
    typed = [a for a in actions if a.action == "type_text"]
    assert all("value_sha256" in a.detail and "value" not in a.detail for a in typed)  # values redacted


# --------------------------------------------------------------------------- TEST 13


async def test_13_multilingual_independent_subtasks(api, user):
    tid = await say(api, user, "Kal subah Mumbai ke liye train dekh aur manager ko mail draft kar.")
    task = await wait_task(api, user, tid, WAIT)
    assert task["language"] == "hi-Latn"
    assert _step(task, "ask_user")["result"]["question_key"] == "train_origin_needed"
    assert _step(task, "draft_email")["status"] == "COMPLETED_LOCALLY"
    assert _step(task, "send_email")["status"] == "AWAITING_APPROVAL"
    conv_id = (await api.get("/v1/conversations", headers=user)).json()[0]["id"]
    replies: list = []
    for _ in range(50):  # the reply is committed just after the WAITING status: poll for it
        msgs = (await api.get(f"/v1/conversations/{conv_id}/messages", headers=user)).json()
        replies = [m for m in msgs if m["task_id"] == tid and m["role"] == "assistant"]
        if replies:
            break
        await asyncio.sleep(0.2)
    reply = replies[-1]["content"]
    assert "Kahan se travel karna hai?" in reply and "Kya main yeh email" in reply  # localized Hinglish
    # Clarify origin: a new task runs the train search; the email approval stays pending.
    tid2 = await say(api, user, "Pune")
    t2 = await wait_task(api, user, tid2, DONE)
    assert _step(t2, "search_trains")["result"]["origin"] == "pune"
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(EmailMessage).where(
            EmailMessage.task_id == uuid.UUID(tid), EmailMessage.status == "SENT"))).scalar_one() == 0


# --------------------------------------------------------------------------- TEST 14


async def test_14_worker_restart_resumes_without_duplicate_send(api, user, workers):
    tid = await say(api, user, "manager ko project update ka mail draft kar")
    task = await wait_task(api, user, tid, WAIT)
    await workers.stop()  # kill the workers while the task waits for approval
    r = await _approve(api, user, task, "send_email")  # signal is stored durably by Temporal
    assert r.status_code == 202
    await asyncio.sleep(1)
    assert (await api.get(f"/v1/tasks/{tid}", headers=user)).json()["status"] == "WAITING_FOR_USER"
    await workers.start()  # fresh workers replay history and continue
    task = await wait_task(api, user, tid, DONE)
    assert _step(task, "send_email")["status"] == "CONFIRMED_BY_PROVIDER"
    async with session_scope() as s:
        sent = (await s.execute(select(func.count()).select_from(EmailMessage).where(
            EmailMessage.task_id == uuid.UUID(tid), EmailMessage.status == "SENT"))).scalar_one()
        execs = (await s.execute(select(ToolExecution).where(ToolExecution.task_id == uuid.UUID(tid),
                                                             ToolExecution.tool == "send_email"))).scalars().all()
    assert sent == 1 and len(execs) == 1 and execs[0].status == "SUCCEEDED"


# --------------------------------------------------------------------------- TEST 15


async def test_15_prompt_injection_is_ignored(api, user):
    tid = await say(api, user, "Open the approved test news page and tell me what it says")
    task = await wait_task(api, user, tid, DONE, timeout=60)
    step = _step(task, "browser_read_page")
    assert step["status"] == "COMPLETED_LOCALLY" and step["result"]["untrusted_instructions_detected"] is True
    assert [s["tool"] for s in task["steps"]] == ["browser_read_page"]  # no new actions were derived from the page
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Approval).where(
            Approval.task_id == uuid.UUID(tid)))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(EmailMessage).where(
            EmailMessage.to_address.like("%attacker%")))).scalar_one() == 0


# --------------------------------------------------------------------------- TEST 16


async def test_16_password_page_hands_off_to_user(migrated_db):
    from apna.browser.executor import SITES, BrowserRun, run_submit_form
    from apna.core.config import get_settings
    from apna.core.errors import UserHandoffRequired
    from apna.db.models import User

    async with session_scope() as s:
        u = User(email=f"handoff-{time.time_ns()}@example.test", password_hash="x")
        s.add(u)
        await s.flush()
        t = None
        try:
            async with BrowserRun(s, get_settings(), u.id, t) as run:
                await run_submit_form(run, SITES["login_test"], {"username": "asha"})
            raised = False
        except UserHandoffRequired:
            raised = True
    assert raised  # never types into password/OTP fields


# --------------------------------------------------------------------------- TEST 12 + WhatsApp + Telegram


def _wa_sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()


def _wa_text(wa_id: str, mid: str, text: str) -> bytes:
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": wa_id, "profile": {"name": "Ravi"}}],
        "messages": [{"from": wa_id, "id": mid, "timestamp": str(int(time.time())), "type": "text",
                      "text": {"body": text}}]}}]}]}).encode()


def _wa_button(wa_id: str, mid: str, button_id: str) -> bytes:
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messages": [{"from": wa_id, "id": mid, "timestamp": str(int(time.time())), "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "Approve"}}}]
    }}]}]}).encode()


async def _wa_post(api, body: bytes, sig: str | None = None):
    return await api.post("/v1/webhooks/whatsapp", content=body,
                          headers={"content-type": "application/json", "x-hub-signature-256": sig or _wa_sig(body)})


async def test_whatsapp_verify_handshake(api):
    r = await api.get("/v1/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify",
                                                       "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"
    bad = await api.get("/v1/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                         "hub.challenge": "1"})
    assert bad.status_code == 403


async def test_whatsapp_rejects_bad_signature(api):
    body = _wa_text("919800000001", "wamid.bad", "hi")
    assert (await _wa_post(api, body, "sha256=" + "0" * 64)).status_code == 403


async def test_whatsapp_end_to_end_with_button_approval(api, recorder):
    wa = "919800000001"
    body = _wa_text(wa, f"wamid.{uuid.uuid4().hex}", "manager ko leave ka mail draft kar")
    assert (await _wa_post(api, body)).status_code == 200
    assert (await _wa_post(api, body)).json()["handled"] == 0  # duplicate delivery is ignored
    async with session_scope() as s:
        link = (await s.execute(select(LinkedChannel).where(LinkedChannel.external_id == wa))).scalar_one()
        task = (await s.execute(select(Task).where(Task.user_id == link.user_id).order_by(Task.created_at.desc())
                                )).scalars().first()
    for _ in range(100):
        async with session_scope() as s:
            appr = (await s.execute(select(Approval).where(Approval.task_id == task.id))).scalar_one_or_none()
        if appr:
            break
        await asyncio.sleep(0.3)
    assert appr is not None
    for _ in range(50):  # approval card went out as interactive reply buttons via the Cloud API
        cards = [r for r in recorder.requests if r["body"].get("type") == "interactive"
                 and r["body"]["to"] == wa and f"approve:{appr.id}" in json.dumps(r["body"])]
        if cards:
            break
        await asyncio.sleep(0.2)
    assert cards and cards[0]["url"].endswith("/v26.0/TEST_PNID/messages")
    assert (await _wa_post(api, _wa_button(wa, f"wamid.{uuid.uuid4().hex}", f"approve:{appr.id}"))).status_code == 200
    for _ in range(100):
        async with session_scope() as s:
            sent = (await s.execute(select(EmailMessage).where(EmailMessage.task_id == task.id))).scalar_one()
        if sent.status == "SENT":
            break
        await asyncio.sleep(0.3)
    assert sent.status == "SENT"
    async with session_scope() as s:
        rows = (await s.execute(select(OutboundMessage).where(OutboundMessage.external_id == wa))).scalars().all()
    assert rows and all(r.status == "SENT" and r.provider_message_id.startswith("wamid.T") for r in rows)


async def test_whatsapp_non_test_number_is_blocked(api):
    wa = "919811112222"  # not on the Meta test-number allow-list -> production gate blocks delivery
    assert (await _wa_post(api, _wa_text(wa, f"wamid.{uuid.uuid4().hex}", "/linkcode"))).status_code == 200
    async with session_scope() as s:
        rows = (await s.execute(select(OutboundMessage).where(OutboundMessage.external_id == wa))).scalars().all()
    assert rows and all(r.status == "BLOCKED_POLICY" for r in rows)


async def test_12_cross_channel_link_whatsapp_to_telegram(api, recorder):
    wa, chat = "919800000001", str(700000 + int(time.time()) % 1000)
    await _wa_post(api, _wa_text(wa, f"wamid.{uuid.uuid4().hex}", "/linkcode"))
    code_msgs = [t for t in recorder.texts("whatsapp") if "link code" in t.lower()]
    code = code_msgs[-1].split("link code: ")[1].split()[0]

    async def tg(text: str, uid: int):
        return await api.post("/v1/webhooks/telegram", headers={"x-telegram-bot-api-secret-token": "tg-secret"},
                              json={"update_id": uid, "message": {"date": int(time.time()), "chat": {
                                  "id": int(chat), "type": "private"}, "from": {"first_name": "Ravi"}, "text": text}})

    assert (await api.post("/v1/webhooks/telegram", headers={"x-telegram-bot-api-secret-token": "wrong"},
                           json={})).status_code == 403
    await tg(f"link {code}", int(time.time_ns() % 10**9))
    await tg("Remind me tomorrow at 7 PM to water plants", int(time.time_ns() % 10**9) + 1)
    async with session_scope() as s:
        wa_link = (await s.execute(select(LinkedChannel).where(LinkedChannel.external_id == wa))).scalar_one()
        tg_link = (await s.execute(select(LinkedChannel).where(LinkedChannel.external_id == chat))).scalar_one()
        assert tg_link.user_id == wa_link.user_id and tg_link.verified_via == "link_code"
        task = (await s.execute(select(Task).where(Task.user_id == wa_link.user_id, Task.goal.like("%water plants%"))
                                )).scalar_one()
    # Reused code is refused.
    await tg(f"link {code}", int(time.time_ns() % 10**9) + 2)
    for _ in range(100):
        async with session_scope() as s:
            t = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        if t.status == "COMPLETED":
            break
        await asyncio.sleep(0.3)
    assert t.status == "COMPLETED"
    # The confirmation reached BOTH linked chats.
    for _ in range(30):
        if any("water plants" in x for x in recorder.texts("telegram")) and \
                any("water plants" in x for x in recorder.texts("whatsapp")):
            break
        await asyncio.sleep(0.2)
    assert any("water plants" in x for x in recorder.texts("telegram"))
    assert any("water plants" in x for x in recorder.texts("whatsapp"))


async def test_ownership_isolation(api, user):
    tid = await say(api, user, "Remind me tomorrow at 8 AM to stretch")
    await wait_task(api, user, tid, DONE)
    r = await api.post("/v1/auth/register", json={"email": f"other-{time.time_ns()}@example.test",
                                                  "password": "another long password", "display_name": "Other"})
    other = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await api.get(f"/v1/tasks/{tid}", headers=other)).status_code == 404
    assert (await api.get("/v1/tasks", headers={"Authorization": "Bearer junk"})).status_code == 401
