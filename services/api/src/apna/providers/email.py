"""Email provider contract + sandbox mailbox implementation.

`SandboxEmailProvider` is a deterministic mock mailbox backed by the `email_messages` table plus a
fixture inbox. It never contacts a mail server; every result carries `environment="mock"`.
Gmail (OAuth, least-privilege scopes) implements the same Protocol later - see
docs/integrations/EMAIL_INTEGRATION.md. Status: WORKING_IN_MOCK.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.db.models import EmailMessage


@dataclass(frozen=True)
class InboxThread:
    thread_ref: str
    from_name: str
    from_address: str
    subject: str
    snippet: str
    received_at: str


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    to_address: str
    subject: str
    body: str
    status: str
    provider: str
    content_hash: str


@dataclass(frozen=True)
class SendReceipt:
    provider: str
    provider_message_id: str
    accepted: bool
    environment: str


def content_hash(to_address: str, subject: str, body: str) -> str:
    return hashlib.sha256(f"{to_address}\n{subject}\n{body}".encode()).hexdigest()


class EmailProvider(Protocol):
    name: str
    environment: str

    async def search_threads(self, session: AsyncSession, user_id: uuid.UUID, query: str) -> list[InboxThread]: ...
    async def resolve_contact(self, user_id: uuid.UUID, hint: str) -> str | None: ...
    async def create_draft(self, session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID | None,
                           to_address: str, subject: str, body: str, thread_ref: str | None) -> DraftRecord: ...
    async def get_draft(self, session: AsyncSession, user_id: uuid.UUID, draft_id: str) -> DraftRecord | None: ...
    async def send_draft(self, session: AsyncSession, user_id: uuid.UUID, draft_id: str,
                         idempotency_key: str) -> SendReceipt: ...


# Fixture data for the sandbox mailbox. Addresses use the reserved `.test` TLD (RFC 2606),
# so nothing here can ever be delivered.
_FIXTURE_INBOX = [
    InboxThread("sandbox-thread-meeting-1", "Priya Sharma (Manager)", "priya.manager@company.test",
                "Project sync meeting next week", "Can we meet next week to review the Q4 plan?",
                "2026-11-11T10:15:00+05:30"),
    InboxThread("sandbox-thread-recruiter-1", "Rahul (Recruiter)", "rahul.recruiter@hiring.test",
                "Interview scheduling", "Please share two slots for a 45-minute interview.",
                "2026-11-10T16:40:00+05:30"),
]
_FIXTURE_CONTACTS = {"manager": "priya.manager@company.test", "recruiter": "rahul.recruiter@hiring.test"}


class SandboxEmailProvider:
    name = "sandbox-email"
    environment = "mock"

    async def search_threads(self, session: AsyncSession, user_id: uuid.UUID, query: str) -> list[InboxThread]:
        q = query.lower().strip()
        if q in {"", "latest"}:
            return list(_FIXTURE_INBOX)
        return [t for t in _FIXTURE_INBOX if q in (t.subject + " " + t.snippet + " " + t.from_name).lower()]

    async def resolve_contact(self, user_id: uuid.UUID, hint: str) -> str | None:
        h = hint.strip().lower()
        if "@" in h:
            known = set(_FIXTURE_CONTACTS.values()) | {t.from_address for t in _FIXTURE_INBOX}
            return h if h in known else None  # only verified contacts; never a guessed address
        return _FIXTURE_CONTACTS.get(h)

    async def create_draft(self, session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID | None,
                           to_address: str, subject: str, body: str, thread_ref: str | None) -> DraftRecord:
        row = EmailMessage(user_id=user_id, task_id=task_id, provider=self.name, status="DRAFT",
                           to_address=to_address, subject=subject, body=body, thread_ref=thread_ref)
        session.add(row)
        await session.flush()
        return self._record(row)

    async def get_draft(self, session: AsyncSession, user_id: uuid.UUID, draft_id: str) -> DraftRecord | None:
        row = (await session.execute(
            select(EmailMessage).where(EmailMessage.id == uuid.UUID(draft_id), EmailMessage.user_id == user_id)
        )).scalar_one_or_none()
        return self._record(row) if row else None

    async def send_draft(self, session: AsyncSession, user_id: uuid.UUID, draft_id: str,
                         idempotency_key: str) -> SendReceipt:
        row = (await session.execute(
            select(EmailMessage).where(EmailMessage.id == uuid.UUID(draft_id), EmailMessage.user_id == user_id)
            .with_for_update()
        )).scalar_one()
        if row.status == "SENT":  # idempotent: second call returns the original receipt
            return SendReceipt(self.name, row.provider_message_id or "", True, self.environment)
        row.status = "SENT"
        row.sent_at = datetime.now(UTC)
        row.provider_message_id = f"sandbox-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        await session.flush()
        return SendReceipt(self.name, row.provider_message_id, True, self.environment)

    @staticmethod
    def _record(row: EmailMessage) -> DraftRecord:
        return DraftRecord(str(row.id), row.to_address, row.subject, row.body, row.status, row.provider,
                           content_hash(row.to_address, row.subject, row.body))
