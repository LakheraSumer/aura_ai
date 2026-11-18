"""Provider login challenges (OTP) answered on a one-time secure page, never in chat.

Rules (see docs/integrations/PLAN.md):
- A challenge is created only by a provider adapter after the provider itself asked for a code. There
  is no generic "send me your OTP" collector.
- Each challenge is bound to one user, provider, purpose and provider session (session_ref), expires
  after 5 minutes and allows 3 attempts.
- The URL token is random and stored only as a SHA-256 hash.
- The code is stored AES-GCM encrypted, bound to the challenge, and wiped when the adapter consumes it.
  It never enters the chat transcript, the planner, logs or workflow history: workflows only ever see the
  challenge id.
- Payment secrets (UPI PIN, card PIN, CVV, bank passwords, payment-authorisation OTPs) are never
  collected, on this page or anywhere else; the purpose is checked against that rule.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.core.errors import Conflict, InvalidInput, NotFound, PolicyViolation
from apna.db.models import AuthChallenge
from apna.security import crypto

TTL = timedelta(minutes=5)
MAX_ATTEMPTS = 3
_FORBIDDEN_PURPOSE = re.compile(r"payment|upi|pin|cvv|card|bank|netbanking|debit|credit|transaction", re.I)
_CODE = re.compile(r"^\d{4,8}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aad(ch: AuthChallenge) -> str:
    return f"{ch.id}:{ch.user_id}:{ch.provider}"


async def create_challenge(session: AsyncSession, *, user_id: uuid.UUID, provider: str, purpose: str,
                           session_ref: str) -> tuple[AuthChallenge, str]:
    """Called by a provider adapter after a real provider login challenge. Returns (challenge, url_token)."""
    if _FORBIDDEN_PURPOSE.search(purpose):
        raise PolicyViolation("Payment secrets are never collected; the user authorises payments in their own app.")
    crypto._key()  # refuse up front if secrets can't be stored encrypted
    token = secrets.token_urlsafe(32)
    ch = AuthChallenge(user_id=user_id, provider=provider, purpose=purpose[:120], session_ref=session_ref[:120],
                       token_hash=_hash(token), attempts=0, max_attempts=MAX_ATTEMPTS, status="PENDING",
                       expires_at=datetime.now(UTC) + TTL)
    session.add(ch)
    await session.flush()
    return ch, token


async def open_challenge(session: AsyncSession, token: str) -> AuthChallenge:
    ch = (await session.execute(select(AuthChallenge).where(
        AuthChallenge.token_hash == _hash(token)).with_for_update())).scalar_one_or_none()
    if ch is None:
        raise NotFound("This link is not valid.")
    if ch.status == "PENDING" and ch.expires_at <= datetime.now(UTC):
        ch.status = "EXPIRED"
    if ch.status != "PENDING":
        raise Conflict(f"This link can't be used any more ({ch.status.lower()}).")
    return ch


async def submit_code(session: AsyncSession, token: str, code: str) -> AuthChallenge:
    ch = await open_challenge(session, token)
    ch.attempts += 1
    code = (code or "").strip()
    if not _CODE.match(code):
        if ch.attempts >= ch.max_attempts:
            ch.status = "LOCKED"
        raise InvalidInput("Enter the numeric code the provider sent you.")
    ch.secret_ciphertext = crypto.encrypt(code, _aad(ch))
    ch.status = "SUBMITTED"
    await session.flush()
    return ch


async def consume_code(session: AsyncSession, challenge_id: uuid.UUID, *, user_id: uuid.UUID,
                       provider: str) -> str:
    """For the provider adapter only: returns the code once, then wipes it."""
    ch = (await session.execute(select(AuthChallenge).where(AuthChallenge.id == challenge_id)
                                .with_for_update())).scalar_one_or_none()
    if ch is None or ch.user_id != user_id or ch.provider != provider:
        raise NotFound("Challenge not found for this user and provider.")
    if ch.status != "SUBMITTED" or not ch.secret_ciphertext:
        raise Conflict(f"Challenge is {ch.status}.")
    if ch.expires_at <= datetime.now(UTC):
        ch.status, ch.secret_ciphertext = "EXPIRED", None
        raise Conflict("Challenge expired.")
    code = crypto.decrypt(ch.secret_ciphertext, _aad(ch))
    ch.secret_ciphertext, ch.status = None, "CONSUMED"
    await session.flush()
    return code
