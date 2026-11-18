"""Payments: Razorpay Payment Links (credentials-required) and payment intents.

How a real payment works (when RAZORPAY_* test or live keys are set):
1. The user approves the exact purchase (action-hash approval).
2. Apna creates a payment link for exactly that amount, keyed by the step's idempotency key
   (`reference_id`), so a retry can't create a second link for the same purchase.
3. The user pays in their own UPI app (GPay / PhonePe / Paytm) or with a card on Razorpay's hosted
   page. Apna never sees a UPI PIN, card number or OTP.
4. Completion is proven only by Razorpay: a webhook whose HMAC-SHA256 signature (X-Razorpay-Signature,
   keyed by the webhook secret, over the raw body) verifies, or the status API. Creating a link is not
   proof of payment.
5. PaymentWorkflow waits for the webhook and polls the status API as a fallback (reconciliation), then
   completes the purchase exactly once, or reports failure/expiry. Nothing is ordered unless paid.

Without keys the gateway is unavailable and the TEST sandbox payment is used instead, clearly labelled.
Docs: https://razorpay.com/docs/api/payments/payment-links/ , https://razorpay.com/docs/webhooks/validate-test
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apna.core.config import Settings
from apna.core.errors import ProviderUnavailable
from apna.db.models import PaymentIntent, WebhookEvent

# Razorpay payment link status -> our intent status
_LINK_STATUS = {"created": "PENDING", "partially_paid": "PENDING", "paid": "PAID", "expired": "EXPIRED",
                "cancelled": "CANCELLED"}
TERMINAL = {"PAID", "FAILED", "EXPIRED", "CANCELLED"}


# Tests inject an httpx.MockTransport here; production uses real HTTPS.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class RazorpayGateway:
    name = "razorpay"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.razorpay_configured():
            raise ProviderUnavailable("Razorpay keys are not configured.")
        self.s = settings
        self._auth = (settings.razorpay_key_id or "", settings.razorpay_key_secret.get_secret_value())  # type: ignore[union-attr]
        self._transport = transport or TRANSPORT

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=self.s.razorpay_api_base_url, auth=self._auth, timeout=20,
                                         transport=self._transport) as c:
                r = await c.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Razorpay unreachable: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            err = (r.json().get("error") or {}).get("description") if r.content else ""
            raise ProviderUnavailable(f"Razorpay error {r.status_code}: {err}")
        return r.json()

    async def create_link(self, *, amount_minor: int, currency: str, description: str, reference_id: str,
                          expire_by: datetime, notes: dict[str, str]) -> dict[str, Any]:
        return await self._call("POST", "/v1/payment_links", json={
            "amount": amount_minor, "currency": currency, "accept_partial": False,
            "description": description[:2048], "reference_id": reference_id[:40],
            "expire_by": int(expire_by.timestamp()), "notify": {"sms": False, "email": False},
            "reminder_enable": False, "notes": notes,
        })

    async def fetch_link(self, link_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/v1/payment_links/{link_id}")


def verify_razorpay_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def record_webhook_once(session: AsyncSession, provider: str, event_id: str) -> bool:
    """True the first time an event id is seen; False for duplicate deliveries."""
    try:
        async with session.begin_nested():
            session.add(WebhookEvent(provider=provider, event_id=event_id))
            await session.flush()
        return True
    except IntegrityError:
        return False


def status_from_link(link: dict[str, Any]) -> str:
    return _LINK_STATUS.get(str(link.get("status")), "PENDING")


async def create_intent(session: AsyncSession, settings: Settings, *, user_id: uuid.UUID, purpose: str,
                        purpose_ref: str, amount_minor: int, currency: str, description: str,
                        idempotency_key: str, gateway: RazorpayGateway | None = None) -> PaymentIntent:
    """Idempotent: the same key returns the existing intent (no second payment link)."""
    existing = (await session.execute(select(PaymentIntent).where(
        PaymentIntent.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing:
        return existing
    gw = gateway or RazorpayGateway(settings)
    expires = datetime.now(UTC) + timedelta(minutes=settings.payment_link_ttl_minutes)
    intent = PaymentIntent(user_id=user_id, gateway=gw.name, purpose=purpose, purpose_ref=purpose_ref,
                           amount_minor=amount_minor, currency=currency, status="CREATED",
                           idempotency_key=idempotency_key, expires_at=expires, gateway_status={})
    session.add(intent)
    await session.flush()  # reserve the key before calling out
    link = await gw.create_link(amount_minor=amount_minor, currency=currency, description=description,
                                reference_id=str(intent.id).replace("-", "")[:40], expire_by=expires,
                                notes={"apna_intent": str(intent.id), "purpose": purpose})
    intent.gateway_ref, intent.payment_url = link.get("id"), link.get("short_url")
    intent.status, intent.gateway_status = status_from_link(link), {"link_status": link.get("status")}
    await session.flush()
    return intent


def apply_status(intent: PaymentIntent, new_status: str, raw: dict[str, Any]) -> bool:
    """Move an intent forward; terminal states never change. Returns True if it changed."""
    intent.last_checked_at = datetime.now(UTC)
    if intent.status in TERMINAL or new_status == intent.status:
        return False
    intent.status, intent.gateway_status = new_status, {**(intent.gateway_status or {}), **raw}
    return True
