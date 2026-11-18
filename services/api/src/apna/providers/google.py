"""Google account connection (OAuth 2.0 + PKCE), Gmail and Google Calendar. CREDENTIALS_REQUIRED.

Needs GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and a stable PUBLIC_BASE_URL whose
/v1/oauth/google/callback is registered as a redirect URI in Google Cloud Console.

- The user signs in on Google's own page; Apna never sees their password or 2-step codes.
- The OAuth state is single-use, 10 minutes, stored hashed; the PKCE verifier is stored encrypted.
- Tokens are encrypted at rest (services.accounts, AAD-bound to the user) and never reach the planner,
  replies or logs. "disconnect google" revokes them.
- Drafts are kept in Apna's own table; sending builds the message from the exact approved draft (never a
  Gmail-side draft that could have been edited after approval).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage as MimeMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.core.config import Settings
from apna.core.errors import ApnaError
from apna.db.models import EmailMessage, OAuthState
from apna.providers.email import DraftRecord, InboxThread, SendReceipt, content_hash
from apna.security import crypto
from apna.services import accounts

TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject httpx.MockTransport
PROVIDER = "google"
SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/calendar.events"]
STATE_TTL = timedelta(minutes=10)


class GoogleNotConnected(ApnaError):
    code = "google_not_connected"
    http_status = 409


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, transport=TRANSPORT)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ------------------------------------------------------------------ OAuth


async def start_connect(session: AsyncSession, settings: Settings, user_id: uuid.UUID) -> str:
    """Google's consent URL for this user (single-use state + PKCE S256)."""
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    session.add(OAuthState(user_id=user_id, provider=PROVIDER, state_hash=_hash(state),
                           verifier_ciphertext=crypto.encrypt(verifier, f"oauth:{user_id}"),
                           expires_at=datetime.now(UTC) + STATE_TTL))
    await session.flush()
    q = {"client_id": settings.google_client_id, "redirect_uri": settings.google_redirect_uri(),
         "response_type": "code", "scope": " ".join(SCOPES), "state": state, "access_type": "offline",
         "prompt": "consent", "include_granted_scopes": "true",
         "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    return f"{settings.google_auth_url}?{urlencode(q)}"


async def finish_connect(session: AsyncSession, settings: Settings, state: str, code: str) -> uuid.UUID:
    row = (await session.execute(select(OAuthState).where(OAuthState.state_hash == _hash(state))
                                 .with_for_update())).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        raise ApnaError("This sign-in link is invalid or expired. Ask the assistant to 'connect Google' again.")
    row.used_at = datetime.now(UTC)  # single use, even if the exchange below fails
    await session.flush()
    verifier = crypto.decrypt(row.verifier_ciphertext, f"oauth:{row.user_id}")
    assert settings.google_client_secret is not None
    async with _http() as c:
        r = await c.post(settings.google_token_url, data={
            "code": code, "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "redirect_uri": settings.google_redirect_uri(), "grant_type": "authorization_code",
            "code_verifier": verifier})
    if r.status_code != 200:
        raise ApnaError("Google didn't accept the sign-in. Please try 'connect Google' again.")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise ApnaError("Google didn't return offline access. Remove Apna from your Google account's "
                        "third-party access and connect again.")
    await _store(session, row.user_id, tok["refresh_token"], tok.get("access_token"), int(tok.get("expires_in", 0)))
    return row.user_id


async def _store(session: AsyncSession, user_id: uuid.UUID, refresh: str, access: str | None, expires_in: int) -> None:
    blob = json.dumps({"refresh_token": refresh, "access_token": access,
                       "expires_at": (datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 0))).isoformat()})
    await accounts.link_account(session, user_id, PROVIDER, blob, kind="oauth", scopes=SCOPES)


async def connected(session: AsyncSession, user_id: uuid.UUID) -> bool:
    return PROVIDER in await accounts.linked_providers(session, user_id)


async def access_token(session: AsyncSession, settings: Settings, user_id: uuid.UUID) -> str:
    raw = await accounts.get_token(session, user_id, PROVIDER)
    if not raw:
        raise GoogleNotConnected("Google isn't connected.")
    t = json.loads(raw)
    if t.get("access_token") and datetime.fromisoformat(t["expires_at"]) > datetime.now(UTC):
        return str(t["access_token"])
    assert settings.google_client_secret is not None
    async with _http() as c:
        r = await c.post(settings.google_token_url, data={
            "client_id": settings.google_client_id, "client_secret": settings.google_client_secret.get_secret_value(),
            "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    if r.status_code != 200:
        await accounts.revoke(session, user_id, PROVIDER)  # refresh token revoked/expired by the user
        raise GoogleNotConnected("Google access was revoked. Say 'connect Google' to connect again.")
    d = r.json()
    await _store(session, user_id, t["refresh_token"], d["access_token"], int(d.get("expires_in", 0)))
    return str(d["access_token"])


async def _api(session: AsyncSession, settings: Settings, user_id: uuid.UUID, method: str, url: str,
               **kw: Any) -> dict[str, Any]:
    tok = await access_token(session, settings, user_id)
    async with _http() as c:
        r = await c.request(method, url, headers={"Authorization": f"Bearer {tok}"}, **kw)
    if r.status_code == 401:
        raise GoogleNotConnected("Google access expired. Say 'connect Google' to connect again.")
    r.raise_for_status()
    return r.json() if r.content else {}


# ------------------------------------------------------------------ Gmail (EmailProvider)


class GmailProvider:
    name = "gmail"
    environment = "live"

    def __init__(self, settings: Settings, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.s, self.session, self.user_id = settings, session, user_id
        self.base = f"{settings.gmail_api_base_url}/gmail/v1/users/me"

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await _api(self.session, self.s, self.user_id, "GET", f"{self.base}{path}", params=params)

    async def search_threads(self, session: AsyncSession, user_id: uuid.UUID, query: str) -> list[InboxThread]:
        q = "" if query.strip().lower() in ("", "latest") else query
        listing = await self._get("/messages", q=f"in:inbox {q}".strip(), maxResults=5)
        out = []
        for m in (listing.get("messages") or [])[:5]:
            d = await self._get(f"/messages/{m['id']}", format="metadata", metadataHeaders=["From", "Subject", "Date"])
            h = {x["name"].lower(): x["value"] for x in (d.get("payload") or {}).get("headers") or []}
            name, addr = parseaddr(h.get("from", ""))
            ts = datetime.fromtimestamp(int(d.get("internalDate", "0")) / 1000, UTC).isoformat()
            out.append(InboxThread(f"gmail:{d.get('threadId')}:{m['id']}", name or addr, addr.lower(),
                                   h.get("subject", "(no subject)"), (d.get("snippet") or "")[:300], ts))
        return out

    async def resolve_contact(self, user_id: uuid.UUID, hint: str) -> str | None:
        """Only an address that actually appears in the user's mailbox; never a guess."""
        h = hint.strip()
        if not h or h == "unknown":
            return None
        q = f"from:{h}" if "@" in h else f'from:"{h}"'
        listing = await self._get("/messages", q=q, maxResults=1)
        msgs = listing.get("messages") or []
        if not msgs:
            return None
        d = await self._get(f"/messages/{msgs[0]['id']}", format="metadata", metadataHeaders=["From"])
        hdr = next((x["value"] for x in (d.get("payload") or {}).get("headers") or [] if x["name"].lower() == "from"), "")
        addr = parseaddr(hdr)[1].lower()
        return addr or None

    async def create_draft(self, session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID | None,
                           to_address: str, subject: str, body: str, thread_ref: str | None) -> DraftRecord:
        row = EmailMessage(user_id=user_id, task_id=task_id, provider=self.name, status="DRAFT",
                           to_address=to_address, subject=subject, body=body, thread_ref=thread_ref)
        session.add(row)
        await session.flush()
        return _record(row)

    async def get_draft(self, session: AsyncSession, user_id: uuid.UUID, draft_id: str) -> DraftRecord | None:
        row = (await session.execute(select(EmailMessage).where(
            EmailMessage.id == uuid.UUID(draft_id), EmailMessage.user_id == user_id))).scalar_one_or_none()
        return _record(row) if row else None

    async def send_draft(self, session: AsyncSession, user_id: uuid.UUID, draft_id: str,
                         idempotency_key: str) -> SendReceipt:
        row = (await session.execute(select(EmailMessage).where(
            EmailMessage.id == uuid.UUID(draft_id), EmailMessage.user_id == user_id).with_for_update())).scalar_one()
        if row.status == "SENT":
            return SendReceipt(self.name, row.provider_message_id or "", True, self.environment)
        mime = MimeMessage()
        mime["To"], mime["Subject"] = row.to_address, row.subject
        mime.set_content(row.body)
        payload: dict[str, Any] = {"raw": _b64(mime.as_bytes())}
        if row.thread_ref and row.thread_ref.startswith("gmail:"):
            payload["threadId"] = row.thread_ref.split(":")[1]
        sent = await _api(session, self.s, user_id, "POST", f"{self.base}/messages/send", json=payload)
        row.status, row.sent_at, row.provider_message_id = "SENT", datetime.now(UTC), str(sent.get("id", ""))[:200]
        await session.flush()
        return SendReceipt(self.name, row.provider_message_id or "", bool(sent.get("id")), self.environment)


def _record(row: EmailMessage) -> DraftRecord:
    return DraftRecord(str(row.id), row.to_address, row.subject, row.body, row.status, row.provider,
                       content_hash(row.to_address, row.subject, row.body))


# ------------------------------------------------------------------ Calendar


async def list_events(session: AsyncSession, settings: Settings, user_id: uuid.UUID, start: datetime,
                      end: datetime) -> list[dict[str, Any]]:
    d = await _api(session, settings, user_id, "GET", f"{settings.calendar_api_base_url}/calendars/primary/events",
                   params={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                           "orderBy": "startTime", "maxResults": 15})
    out = []
    for e in d.get("items") or []:
        st, en = e.get("start") or {}, e.get("end") or {}
        out.append({"title": (e.get("summary") or "(no title)")[:120], "start": st.get("dateTime") or st.get("date"),
                    "end": en.get("dateTime") or en.get("date"), "all_day": "date" in st,
                    "location": (e.get("location") or "")[:120]})
    return out


async def create_event(session: AsyncSession, settings: Settings, user_id: uuid.UUID, *, title: str,
                       start: datetime, end: datetime, tz: str, request_id: str) -> dict[str, Any]:
    """No attendees: creating an event never emails anyone else."""
    eid = hashlib.sha256(request_id.encode()).hexdigest()[:32]  # stable id: a retry can't create a duplicate
    base = f"{settings.calendar_api_base_url}/calendars/primary/events"
    try:
        d = await _api(session, settings, user_id, "POST", base, params={"sendUpdates": "none"},
                       json={"summary": title, "start": {"dateTime": start.isoformat(), "timeZone": tz},
                             "end": {"dateTime": end.isoformat(), "timeZone": tz}, "id": eid})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:  # 409 = already created by an earlier attempt
            raise
        d = await _api(session, settings, user_id, "GET", f"{base}/{eid}")
    return {"event_id": d.get("id"), "link": d.get("htmlLink"), "title": d.get("summary"),
            "start": (d.get("start") or {}).get("dateTime")}
