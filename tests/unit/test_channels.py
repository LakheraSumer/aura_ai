import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
from apna.channels import telegram, whatsapp
from apna.core.config import Settings

WA = Settings(whatsapp_enabled=True, whatsapp_phone_number_id="PNID", whatsapp_access_token="tok",
              whatsapp_app_secret="sec", whatsapp_allowed_test_numbers=["919800000001"])


def test_signature_verification():
    body = b'{"object":"whatsapp_business_account"}'
    good = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    assert whatsapp.verify_signature("sec", body, good)
    assert not whatsapp.verify_signature("sec", body + b" ", good)
    assert not whatsapp.verify_signature("sec", body, None)
    assert not whatsapp.verify_signature("sec", body, "sha1=abc")


def test_parse_text_button_and_status():
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "919800000001", "profile": {"name": "Asha"}}],
        "messages": [
            {"from": "919800000001", "id": "wamid.1", "timestamp": "1762920000", "type": "text",
             "text": {"body": "kal 8 baje yaad dila"}},
            {"from": "919800000001", "id": "wamid.2", "timestamp": "1762920001", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": "approve:abc", "title": "Approve"}}},
            {"from": "919800000001", "id": "wamid.3", "timestamp": "1762920002", "type": "image", "image": {}}],
        "statuses": [{"id": "wamid.out", "status": "delivered", "recipient_id": "919800000001"}]}}]}]}
    evs = whatsapp.parse_webhook(payload)
    assert [e.kind for e in evs] == ["text", "button", "unsupported", "status"]
    assert evs[0].display_name == "Asha" and evs[1].button_id == "approve:abc" and evs[3].status == "delivered"


def test_payload_window_and_buttons():
    a = whatsapp.WhatsAppAdapter(WA)
    recent = datetime.now(UTC) - timedelta(hours=1)
    kind, body = a.build_payload("919800000001", "Send?", [{"type": "approval", "approval_id": "A1"}],
                                 last_inbound_at=recent, proactive=False)
    assert kind == "session" and body["type"] == "interactive"
    ids = [b["reply"]["id"] for b in body["interactive"]["action"]["buttons"]]
    assert ids == ["approve:A1", "reject:A1"]
    # Outside the 24h window without an approved template: refuse rather than force a free-form send.
    assert a.build_payload("919800000001", "hi", [], last_inbound_at=recent - timedelta(days=2), proactive=True) is None


async def test_production_gate_blocks_non_test_numbers():
    out = await whatsapp.WhatsAppAdapter(WA).send("919999999999", "hi", [], last_inbound_at=datetime.now(UTC),
                                                  proactive=False)
    assert out.status == "BLOCKED_POLICY"


async def test_send_records_provider_message_id():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.OUT1"}]})

    out = await whatsapp.WhatsAppAdapter(WA, httpx.MockTransport(handler)).send(
        "919800000001", "hello", [], last_inbound_at=datetime.now(UTC), proactive=False)
    assert out.status == "SENT" and out.provider_message_id == "wamid.OUT1"
    assert seen["url"] == "https://graph.facebook.com/v26.0/PNID/messages" and seen["auth"] == "Bearer tok"
    assert seen["body"]["text"]["body"] == "hello"


async def test_send_without_credentials_is_reported_not_faked():
    s = Settings(whatsapp_enabled=False, whatsapp_allowed_test_numbers=["919800000001"])
    out = await whatsapp.WhatsAppAdapter(s).send("919800000001", "hi", [], last_inbound_at=datetime.now(UTC),
                                                 proactive=False)
    assert out.status == "SKIPPED_NO_CREDENTIALS"


def test_telegram_parse_and_secret():
    assert telegram.verify_secret("s3cret", "s3cret") and not telegram.verify_secret("s3cret", None)
    evs = telegram.parse_update({"update_id": 5, "message": {"date": 1762920000, "chat": {"id": 42, "type": "private"},
                                                             "from": {"first_name": "Ravi"}, "text": "hello"}})
    assert evs[0].external_id == "42" and evs[0].text == "hello" and evs[0].provider_message_id == "tg:5"
    assert telegram.parse_update({"update_id": 6, "message": {"chat": {"id": -1, "type": "group"}, "text": "x"}}) == []
    cb = telegram.parse_update({"update_id": 7, "callback_query": {"from": {"id": 42}, "data": "reject:Z",
                                                                   "message": {"chat": {"id": 42}}}})
    assert cb[0].kind == "button" and cb[0].button_id == "reject:Z"


def test_telegram_inline_keyboard():
    body = telegram.TelegramAdapter(Settings()).build_payload("42", "Send?", [{"type": "approval", "approval_id": "Q"}])
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:Q"


def test_webhook_only_app_exposes_only_webhooks(monkeypatch):
    from apna.api import main
    from apna.core.config import get_settings
    from fastapi.testclient import TestClient

    def statuses() -> dict[str, int]:
        c = TestClient(main.create_app())  # no `with`: lifespan (Temporal) is not started
        return {
            "register": c.post("/v1/auth/register", json={}).status_code,
            "chat": c.post("/v1/chat/messages", json={}).status_code,
            "docs": c.get("/docs").status_code,
            "wa_verify": c.get("/v1/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                               "hub.challenge": "1"}).status_code,
        }

    monkeypatch.setenv("WEBHOOK_ONLY", "true")
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "right-token")
    get_settings.cache_clear()
    try:
        public = statuses()
    finally:
        monkeypatch.delenv("WEBHOOK_ONLY")
        get_settings.cache_clear()
    assert public["register"] == public["chat"] == public["docs"] == 404
    assert public["wa_verify"] == 403  # webhook route is mounted and checks the verify token
    full = statuses()
    assert full["register"] != 404 and full["docs"] == 200


def test_telegram_button_tap_is_acknowledged_in_webhook_reply():
    ack = telegram.callback_ack({"update_id": 7, "callback_query": {"id": "cq1", "data": "approve:A1",
                                                                     "from": {"id": 5}}})
    assert ack == {"method": "answerCallbackQuery", "callback_query_id": "cq1", "text": "Approved ✅"}
    assert telegram.callback_ack({"update_id": 8, "message": {"chat": {"type": "private", "id": 5}}}) is None
