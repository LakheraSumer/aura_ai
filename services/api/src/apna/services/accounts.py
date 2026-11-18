"""Per-user, per-provider account links with encrypted tokens and revocation.

Only provider adapters call get_token(); tokens never go to the planner, replies or logs. A token is
encrypted with associated data "<user_id>:<provider>", so one user's token can't be read as another's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.db.models import ProviderAccount
from apna.security import crypto


def _aad(user_id: uuid.UUID, provider: str) -> str:
    return f"{user_id}:{provider}"


async def link_account(session: AsyncSession, user_id: uuid.UUID, provider: str, token: str, *,
                       kind: str = "oauth", scopes: list[str] | None = None) -> ProviderAccount:
    row = (await session.execute(select(ProviderAccount).where(
        ProviderAccount.user_id == user_id, ProviderAccount.provider == provider))).scalar_one_or_none()
    if row is None:
        row = ProviderAccount(user_id=user_id, provider=provider, status="LINKED", token_kind=kind, scopes=[])
        session.add(row)
    row.token_ciphertext = crypto.encrypt(token, _aad(user_id, provider))
    row.status, row.token_kind, row.scopes = "LINKED", kind, list(scopes or [])
    row.linked_at, row.revoked_at = datetime.now(UTC), None
    await session.flush()
    return row


async def get_token(session: AsyncSession, user_id: uuid.UUID, provider: str) -> str | None:
    row = (await session.execute(select(ProviderAccount).where(
        ProviderAccount.user_id == user_id, ProviderAccount.provider == provider,
        ProviderAccount.status == "LINKED"))).scalar_one_or_none()
    if row is None or not row.token_ciphertext:
        return None
    return crypto.decrypt(row.token_ciphertext, _aad(user_id, provider))


async def revoke(session: AsyncSession, user_id: uuid.UUID, provider: str) -> bool:
    row = (await session.execute(select(ProviderAccount).where(
        ProviderAccount.user_id == user_id, ProviderAccount.provider == provider))).scalar_one_or_none()
    if row is None or row.status == "REVOKED":
        return False
    row.status, row.token_ciphertext, row.revoked_at = "REVOKED", None, datetime.now(UTC)
    await session.flush()
    return True


async def linked_providers(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await session.execute(select(ProviderAccount.provider).where(
        ProviderAccount.user_id == user_id, ProviderAccount.status == "LINKED"))
    return sorted(r[0] for r in rows)
