"""Versioned REST API (/v1). Chat channels are primary; this API serves testing, admin tooling and
channel linking. Every query is scoped to the authenticated user (row ownership)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apna.api.deps import DB, Conversations, CurrentUser
from apna.core.config import get_settings
from apna.core.errors import Conflict, NotFound, Unauthorized
from apna.db.models import (
    Approval,
    AuthSession,
    Message,
    Notification,
    Task,
    User,
    UserProfile,
    WorkflowEvent,
)
from apna.db.session import session_scope
from apna.security.auth import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from apna.services.conversation import issue_link_code

router = APIRouter(prefix="/v1")


# --------------------------------------------------------------------------- auth


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = "Asia/Kolkata"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int


async def _issue(db: DB, user: User, request: Request) -> TokenOut:
    access, ttl = create_access_token(user.id)
    refresh, refresh_hash = new_refresh_token()
    db.add(AuthSession(user_id=user.id, refresh_token_hash=refresh_hash,
                       expires_at=datetime.now(UTC) + timedelta(seconds=get_settings().jwt_refresh_ttl_seconds),
                       user_agent=(request.headers.get("user-agent") or "")[:400]))
    return TokenOut(access_token=access, refresh_token=refresh, expires_in=ttl)


@router.post("/auth/register", response_model=TokenOut, status_code=201)
async def register(body: RegisterIn, db: DB, request: Request) -> TokenOut:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise Conflict("Unknown timezone") from exc
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("Email already registered") from exc
    db.add(UserProfile(user_id=user.id, display_name=body.display_name, timezone=body.timezone))
    return await _issue(db, user, request)


@router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn, db: DB, request: Request) -> TokenOut:
    user = (await db.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if user is None or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise Unauthorized("Invalid email or password")
    return await _issue(db, user, request)


@router.post("/auth/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, db: DB, request: Request) -> TokenOut:
    sess = (await db.execute(select(AuthSession).where(
        AuthSession.refresh_token_hash == hash_refresh_token(body.refresh_token)).with_for_update())
    ).scalar_one_or_none()
    if sess is None or sess.revoked_at or sess.expires_at <= datetime.now(UTC):
        raise Unauthorized("Invalid refresh token")
    sess.revoked_at = datetime.now(UTC)  # rotation: each refresh token is single-use
    user = (await db.execute(select(User).where(User.id == sess.user_id))).scalar_one()
    return await _issue(db, user, request)


# --------------------------------------------------------------------------- chat


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@router.post("/chat/messages", status_code=202)
async def post_message(body: ChatIn, user: CurrentUser, db: DB, conv: Conversations) -> dict[str, Any]:
    return await conv.handle_text(db, user, "api", body.text)


@router.get("/conversations")
async def list_conversations(user: CurrentUser, db: DB) -> list[dict[str, Any]]:
    from apna.db.models import Conversation

    rows = (await db.execute(select(Conversation).where(Conversation.user_id == user.id)
                             .order_by(Conversation.created_at.desc()))).scalars()
    return [{"id": str(c.id), "title": c.title, "created_at": c.created_at.isoformat()} for c in rows]


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: uuid.UUID, user: CurrentUser, db: DB) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Message).where(Message.conversation_id == conversation_id,
                                                   Message.user_id == user.id).order_by(Message.created_at))).scalars()
    return [{"id": str(m.id), "role": m.role, "channel": m.channel, "content": m.content, "task_id":
             str(m.task_id) if m.task_id else None, "payload": m.payload, "created_at": m.created_at.isoformat()}
            for m in rows]


# --------------------------------------------------------------------------- tasks


def _task_out(t: Task) -> dict[str, Any]:
    return {
        "id": str(t.id), "status": t.status, "goal": t.goal, "language": t.language, "planner": t.planner,
        "error": t.error, "created_at": t.created_at.isoformat(),
        "steps": [{"key": s.step_key, "tool": s.tool, "description": s.description, "status": s.status,
                   "risk_level": s.risk_level, "depends_on": s.depends_on, "result": s.result, "error": s.error}
                  for s in t.steps],
    }


async def _own_task(db: DB, user: User, task_id: uuid.UUID) -> Task:
    t = (await db.execute(select(Task).where(Task.id == task_id, Task.user_id == user.id))).scalar_one_or_none()
    if t is None:
        raise NotFound("Task not found")
    return t


@router.get("/tasks")
async def list_tasks(user: CurrentUser, db: DB, status: str | None = None) -> list[dict[str, Any]]:
    q = select(Task).where(Task.user_id == user.id).order_by(Task.created_at.desc()).limit(50)
    if status:
        q = q.where(Task.status == status)
    return [_task_out(t) for t in (await db.execute(q)).scalars()]


@router.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    return _task_out(await _own_task(db, user, task_id))


@router.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel(task_id: uuid.UUID, user: CurrentUser, db: DB, conv: Conversations) -> dict[str, Any]:
    from apna.workflows.definitions import TaskWorkflow

    t = await _own_task(db, user, task_id)
    await conv.temporal.get_workflow_handle(t.workflow_id).signal(TaskWorkflow.cancel)
    return {"status": "cancel_requested"}


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: uuid.UUID, user: CurrentUser, db: DB, request: Request) -> StreamingResponse:
    """Server-sent events of the task's workflow event log (polls the projection)."""
    await _own_task(db, user, task_id)

    async def stream():  # noqa: ANN202
        last = 0
        for _ in range(600):
            if await request.is_disconnected():
                return
            async with session_scope() as s:
                rows = (await s.execute(select(WorkflowEvent).where(
                    WorkflowEvent.task_id == task_id, WorkflowEvent.seq > last).order_by(WorkflowEvent.seq))).scalars()
                for ev in rows:
                    last = ev.seq
                    yield f"id: {ev.seq}\nevent: {ev.kind}\ndata: {json.dumps(ev.data, default=str)}\n\n"
                    if ev.kind == "task_status" and ev.data.get("to") in {
                            "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED", "EXPIRED"}:
                        return
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


class DecisionIn(BaseModel):
    approval_id: uuid.UUID
    decision: Literal["APPROVED", "REJECTED"]


@router.post("/tasks/{task_id}/approve", status_code=202)
async def approve(task_id: uuid.UUID, body: DecisionIn, user: CurrentUser, db: DB,
                  conv: Conversations) -> dict[str, Any]:
    await _own_task(db, user, task_id)
    appr = await conv.decide(db, user.id, body.approval_id, body.decision, evidence={"channel": "api"})
    if appr.task_id != task_id:
        raise NotFound("Approval not found")
    return {"approval_id": str(appr.id), "status": appr.status}


@router.get("/approvals")
async def list_approvals(user: CurrentUser, db: DB) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Approval).where(Approval.user_id == user.id, Approval.status == "PENDING")
                             .order_by(Approval.created_at.desc()))).scalars()
    return [{"id": str(a.id), "task_id": str(a.task_id), "tool": a.tool, "summary": a.operation_summary,
             "recipient": a.recipient, "expires_at": a.expires_at.isoformat()} for a in rows]


@router.get("/notifications")
async def notifications(user: CurrentUser, db: DB) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Notification).where(Notification.user_id == user.id)
                             .order_by(Notification.created_at.desc()).limit(50))).scalars()
    return [{"id": str(n.id), "kind": n.kind, "title": n.title, "body": n.body, "data": n.data,
             "created_at": n.created_at.isoformat()} for n in rows]


@router.post("/channels/link-code")
async def link_code(user: CurrentUser, db: DB) -> dict[str, str]:
    """Send `link <CODE>` from WhatsApp/Telegram within 10 minutes to attach that chat to this account."""
    return {"code": await issue_link_code(db, user.id), "expires_in_seconds": "600"}
