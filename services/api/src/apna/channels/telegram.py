"""Telegram Bot API adapter - the launchable chat channel (bots / AI assistants are permitted).

Inbound updates are accepted only with the `X-Telegram-Bot-Api-Secret-Token` header that we set
when registering the webhook (setWebhook secret_token).
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any

import httpx

from apna.channels.base import InboundEvent, SendOutcome, approval_buttons
from apna.core.config import Settings


def verify_secret(expected: str, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(expected, header or "")


def parse_update(update: dict[str, Any]) -> list[InboundEvent]:
    uid = str(update.get("update_id", ""))
    if msg := update.get("message"):
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return []  # group chats are out of scope for a personal assistant
        sender = msg.get("from", {})
        ts = datetime.fromtimestamp(msg["date"], UTC) if msg.get("date") else None
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or None
        if "text" in msg:
            return [InboundEvent("telegram", str(chat["id"]), f"tg:{uid}", "text", text=msg["text"],
                                 display_name=name, timestamp=ts, raw_type="text")]
        return [InboundEvent("telegram", str(chat["id"]), f"tg:{uid}", "unsupported", display_name=name,
                             timestamp=ts, raw_type="other")]
    if cq := update.get("callback_query"):
        chat_id = str((cq.get("message") or {}).get("chat", {}).get("id") or cq["from"]["id"])
        return [InboundEvent("telegram", chat_id, f"tg:{uid}", "button", button_id=cq.get("data"),
                             text=cq.get("data", ""), raw_type="callback_query")]
    return []


def callback_ack(update: dict[str, Any]) -> dict[str, Any] | None:
    """answerCallbackQuery as a webhook reply body (stops the button's loading spinner)."""
    cq = update.get("callback_query")
    if not cq or not cq.get("id"):
        return None
    data = str(cq.get("data", ""))
    text = {"approve": "Approved ✅", "reject": "Rejected", "rdone": "Done ✅", "rsnooze": "Snoozed ⏰"}.get(
        data.partition(":")[0], "")
    return {"method": "answerCallbackQuery", "callback_query_id": cq["id"], "text": text}


class TelegramAdapter:
    name = "telegram"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings
        self._transport = transport

    def build_payload(self, external_id: str, text: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {"chat_id": external_id, "text": text[:4096], "disable_web_page_preview": True}
        buttons = approval_buttons(actions)
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": b} for b, t in buttons]]}
        return body

    async def send(self, external_id: str, text: str, actions: list[dict[str, Any]], *,
                   last_inbound_at: datetime | None, proactive: bool) -> SendOutcome:
        if not (self.s.telegram_enabled and self.s.telegram_bot_token):
            return SendOutcome("SKIPPED_NO_CREDENTIALS", error="Telegram bot token not configured.")
        url = f"{self.s.telegram_api_base_url}/bot{self.s.telegram_bot_token.get_secret_value()}/sendMessage"
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            resp = await client.post(url, json=self.build_payload(external_id, text, actions))
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not data.get("ok"):
            return SendOutcome("FAILED", error=f"{resp.status_code} {data.get('description')}")
        return SendOutcome("SENT", provider_message_id=f"tg:{data['result']['message_id']}")
