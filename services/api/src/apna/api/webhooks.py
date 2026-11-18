"""Channel webhooks: WhatsApp Cloud API and Telegram Bot API. Signature-verified before parsing."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response

from apna.api.deps import DB, Conversations
from apna.channels import telegram, whatsapp
from apna.core.config import get_settings
from apna.core.errors import ApnaError, Conflict, Forbidden, NotFound, ProviderUnavailable
from apna.core.logging import log

router = APIRouter(prefix="/v1/webhooks")


@router.get("/whatsapp")
async def whatsapp_verify(mode: Annotated[str, Query(alias="hub.mode")],
                          token: Annotated[str, Query(alias="hub.verify_token")],
                          challenge: Annotated[str, Query(alias="hub.challenge")]) -> Response:
    s = get_settings()
    if not (s.whatsapp_enabled and s.whatsapp_verify_token):
        raise NotFound("WhatsApp channel disabled")
    if mode == "subscribe" and token == s.whatsapp_verify_token.get_secret_value():
        return Response(challenge, media_type="text/plain")
    raise Forbidden("Verification failed")


@router.post("/whatsapp")
async def whatsapp_inbound(request: Request, db: DB, conv: Conversations,
                           x_hub_signature_256: Annotated[str | None, Header()] = None) -> dict[str, int]:
    s = get_settings()
    if not (s.whatsapp_enabled and s.whatsapp_app_secret):
        raise ProviderUnavailable("WhatsApp channel disabled")
    raw = await request.body()
    if not whatsapp.verify_signature(s.whatsapp_app_secret.get_secret_value(), raw, x_hub_signature_256):
        raise Forbidden("Invalid signature")
    events = whatsapp.parse_webhook(json.loads(raw))
    return await _handle(db, conv, events)


@router.post("/telegram")
async def telegram_inbound(request: Request, db: DB, conv: Conversations,
                           x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    s = get_settings()
    if not (s.telegram_enabled and s.telegram_webhook_secret):
        raise ProviderUnavailable("Telegram channel disabled")
    if not telegram.verify_secret(s.telegram_webhook_secret.get_secret_value(), x_telegram_bot_api_secret_token):
        raise Forbidden("Invalid secret token")
    update = await request.json()
    result = await _handle(db, conv, telegram.parse_update(update))
    # A button tap leaves a spinner on the user's button until answered. Telegram accepts a Bot API
    # call as the webhook response body, so acknowledge it here without a second HTTP request.
    if ack := telegram.callback_ack(update):
        return ack
    return result


async def _handle(db: DB, conv: Conversations, events: list) -> dict[str, int]:
    handled = 0
    for ev in events:
        try:
            await conv.handle_inbound(db, ev)
            handled += 1
        except Conflict:
            await db.rollback()  # duplicate delivery or stale button: acknowledge, do nothing
        except ApnaError as exc:
            await db.rollback()
            log.warning("inbound_rejected", channel=ev.channel, code=exc.code)
    # Always 200 for authenticated deliveries so the provider doesn't retry storms.
    return {"received": len(events), "handled": handled}


_RZP_EVENT_STATUS = {"payment_link.paid": "PAID", "payment_link.expired": "EXPIRED",
                     "payment_link.cancelled": "CANCELLED"}


@router.post("/razorpay")
async def razorpay_inbound(request: Request, db: DB,
                           x_razorpay_signature: Annotated[str | None, Header()] = None,
                           x_razorpay_event_id: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """Razorpay payment events. Signature over the raw body first; duplicates acknowledged and ignored."""
    import hashlib

    from sqlalchemy import select

    from apna.db.models import PaymentIntent
    from apna.providers import payments

    s = get_settings()
    if not s.razorpay_configured():
        raise ProviderUnavailable("Payment gateway not configured")
    raw = await request.body()
    if not payments.verify_razorpay_signature(raw, x_razorpay_signature,
                                              s.razorpay_webhook_secret.get_secret_value()):  # type: ignore[union-attr]
        raise Forbidden("Invalid signature")
    body = json.loads(raw)
    event_id = x_razorpay_event_id or body.get("id") or hashlib.sha256(raw).hexdigest()
    if not await payments.record_webhook_once(db, "razorpay", event_id):
        return {"duplicate": True}
    status = _RZP_EVENT_STATUS.get(body.get("event", ""))
    link = ((body.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
    intent = None
    if link.get("id"):
        intent = (await db.execute(select(PaymentIntent).where(PaymentIntent.gateway_ref == link["id"])
                                   .with_for_update())).scalar_one_or_none()
    if intent is not None and status:
        payment = ((body.get("payload") or {}).get("payment") or {}).get("entity") or {}
        payments.apply_status(intent, status, {"webhook_event": body.get("event"),
                                               "payment_ids": [payment["id"]] if payment.get("id") else []})
        await db.commit()
        try:  # fast path; the workflow also polls the status API, so a failed signal isn't fatal
            await request.app.state.temporal.get_workflow_handle(f"payment-{intent.id}").signal("payment_event")
        except Exception as exc:  # noqa: BLE001
            log.warning("payment_signal_failed", intent=str(intent.id), error=type(exc).__name__)
    return {"ok": True, "matched": intent is not None}
