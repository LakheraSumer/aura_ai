"""WhatsApp Cloud API adapter (official Meta Graph API only).

Compliance (docs/compliance/WHATSAPP_COMPLIANCE.md):
- Since 2026-01-15 Meta's WhatsApp Business Solution terms bar general-purpose AI assistants.
  Production delivery is therefore gated by `whatsapp_production_enabled` (default False). With
  it off, the adapter only talks to numbers in `whatsapp_allowed_test_numbers` (Meta test-number
  development mode) and refuses everything else with BLOCKED_POLICY.
- Free-form messages only inside the 24-hour customer-service window. Outside it, only an approved
  template may be sent; without one the message is not sent (BLOCKED_POLICY), never forced.
- No WhatsApp Web automation, unofficial libraries or consumer numbers - ever.
- Inbound webhooks are accepted only with a valid X-Hub-Signature-256 HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from apna.channels.base import InboundEvent, SendOutcome, approval_buttons
from apna.core.config import Settings

SESSION_WINDOW = timedelta(hours=24)
_BODY_LIMIT = 1024  # interactive body text limit
_TEXT_LIMIT = 4096


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict[str, Any]) -> list[InboundEvent]:
    events: list[InboundEvent] = []
    if payload.get("object") != "whatsapp_business_account":
        return events
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                ts = datetime.fromtimestamp(int(m.get("timestamp", "0")), UTC) if m.get("timestamp") else None
                base = {"channel": "whatsapp", "external_id": m["from"], "provider_message_id": m["id"],
                        "display_name": names.get(m["from"]), "timestamp": ts, "raw_type": m.get("type")}
                if m.get("type") == "text":
                    events.append(InboundEvent(kind="text", text=m["text"]["body"], **base))
                elif m.get("type") == "interactive":
                    inter = m["interactive"]
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    events.append(InboundEvent(kind="button", button_id=reply.get("id"),
                                               text=reply.get("title", ""), **base))
                elif m.get("type") == "button":  # quick-reply button on a template
                    events.append(InboundEvent(kind="button", button_id=m["button"].get("payload"),
                                               text=m["button"].get("text", ""), **base))
                else:
                    events.append(InboundEvent(kind="unsupported", **base))
            for s in value.get("statuses", []):
                events.append(InboundEvent(channel="whatsapp", external_id=s.get("recipient_id", ""),
                                           provider_message_id=s["id"], kind="status", status=s.get("status"),
                                           errors=s.get("errors", [])))
    return events


class WhatsAppAdapter:
    name = "whatsapp"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = settings
        self._transport = transport

    def _configured(self) -> bool:
        return bool(self.s.whatsapp_enabled and self.s.whatsapp_phone_number_id and self.s.whatsapp_access_token)

    def _policy_allows(self, external_id: str) -> bool:
        if self.s.whatsapp_production_enabled:
            return True
        return external_id in set(self.s.whatsapp_allowed_test_numbers)

    def build_payload(self, external_id: str, text: str, actions: list[dict[str, Any]], *,
                      last_inbound_at: datetime | None, proactive: bool) -> tuple[str, dict[str, Any]] | None:
        in_window = bool(last_inbound_at and datetime.now(UTC) - last_inbound_at < SESSION_WINDOW)
        if not in_window:
            if not self.s.whatsapp_reminder_template:
                return None
            return "template", {
                "messaging_product": "whatsapp", "to": external_id, "type": "template",
                "template": {"name": self.s.whatsapp_reminder_template,
                             "language": {"code": self.s.whatsapp_template_language},
                             "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1000]}]}]},
            }
        buttons = approval_buttons(actions)
        if buttons:
            return "session", {
                "messaging_product": "whatsapp", "recipient_type": "individual", "to": external_id,
                "type": "interactive",
                "interactive": {"type": "button", "body": {"text": text[:_BODY_LIMIT]},
                                "action": {"buttons": [{"type": "reply", "reply": {"id": bid, "title": title}}
                                                       for bid, title in buttons]}},
            }
        return "session", {"messaging_product": "whatsapp", "recipient_type": "individual", "to": external_id,
                           "type": "text", "text": {"preview_url": False, "body": text[:_TEXT_LIMIT]}}

    async def send(self, external_id: str, text: str, actions: list[dict[str, Any]], *,
                   last_inbound_at: datetime | None, proactive: bool) -> SendOutcome:
        if not self._policy_allows(external_id):
            return SendOutcome("BLOCKED_POLICY", error="WhatsApp production delivery is disabled for this product "
                                                       "(Meta general-purpose AI restriction); number not on test list.")
        built = self.build_payload(external_id, text, actions, last_inbound_at=last_inbound_at, proactive=proactive)
        if built is None:
            return SendOutcome("BLOCKED_POLICY", error="Outside the 24h window and no approved template configured.")
        kind, body = built
        if not self._configured():
            return SendOutcome("SKIPPED_NO_CREDENTIALS", kind=kind, error="WhatsApp credentials not configured.")
        assert self.s.whatsapp_access_token is not None
        url = f"{self.s.whatsapp_graph_base_url}/{self.s.whatsapp_graph_version}/{self.s.whatsapp_phone_number_id}/messages"
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            resp = await client.post(url, json=body, headers={
                "Authorization": f"Bearer {self.s.whatsapp_access_token.get_secret_value()}"})
        if resp.status_code >= 400:
            err = resp.json().get("error", {}) if resp.headers.get("content-type", "").startswith("application/json") \
                else {}
            return SendOutcome("FAILED", kind=kind, error=f"{resp.status_code} {err.get('code')} {err.get('message')}")
        msgs = resp.json().get("messages") or []
        if not msgs or not msgs[0].get("id"):
            return SendOutcome("FAILED", kind=kind, error="Provider response had no message id.")
        # Accepted by Meta. Delivery/read arrive later as status webhooks.
        return SendOutcome("SENT", provider_message_id=msgs[0]["id"], kind=kind)
