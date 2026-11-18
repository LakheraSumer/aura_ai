"""Conversation service: one backend for every channel (WhatsApp, Telegram, API).

Responsibilities: identity resolution for channel senders, inbound dedupe, task creation and
workflow start, clarification follow-ups, and approval decisions (button or exact short "yes").
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from apna.agent.assist import OFFER_QUESTIONS, offer_request, resolve_offer_answer
from apna.agent.commerce import GROCERY_QUESTIONS, grocery_request, resolve_grocery_answer
from apna.agent.executors import CINEMA_QUESTIONS, resolve_cinema_choice
from apna.agent.language import detect_language
from apna.agent.replies import KNOWN_QUESTIONS
from apna.channels.base import InboundEvent
from apna.core.config import get_settings
from apna.core.errors import Conflict, Forbidden, NotFound
from apna.core.logging import log as _log
from apna.db.models import (
    Approval,
    AuditEvent,
    ChannelLinkCode,
    Conversation,
    LinkedChannel,
    Message,
    OutboundMessage,
    Reminder,
    Task,
    TaskStep,
    User,
    UserProfile,
)
from apna.domain.enums import ApprovalStatus, StepStatus, TaskStatus
from apna.providers.movies import learn_from_text
from apna.providers.travel import find_cities
from apna.security.chat_redaction import redact
from apna.workflows.definitions import DEFAULT_QUEUE, ReminderWorkflow, TaskWorkflow

_AFFIRM = re.compile(r"^\s*(yes|y|ok|okay|approve|approved|send it|send|haan|ha|haa|haan bhej do|bhej do|"
                     r"हाँ|हां|भेज दो|हो|पाठव)\s*[.!]*\s*$", re.I)
_DENY = re.compile(r"^\s*(no|n|reject|cancel|don'?t send|nahi|mat bhejo|नहीं|नको)\s*[.!]*\s*$", re.I)
_LINK = re.compile(r"^\s*/?link\s+([A-Z0-9]{8})\s*$", re.I)


# --------------------------------------------------------------------------- identity


async def resolve_channel_user(session: AsyncSession, ev: InboundEvent) -> tuple[User, LinkedChannel]:
    link = (await session.execute(
        select(LinkedChannel).where(LinkedChannel.channel == ev.channel, LinkedChannel.external_id == ev.external_id)
    )).scalar_one_or_none()
    if link is None:
        # First contact from a provider-verified sender: new channel-native account. Never merged with
        # any other account by phone/email; merging needs a link code (see link_channel).
        user = User(email=None, password_hash=None)
        session.add(user)
        await session.flush()
        session.add(UserProfile(user_id=user.id, display_name=(ev.display_name or "Friend")[:120],
                                timezone=get_settings().default_timezone))
        link = LinkedChannel(user_id=user.id, channel=ev.channel, external_id=ev.external_id,
                             display_name=ev.display_name, verified_via="channel_inbound")
        session.add(link)
        session.add(AuditEvent(user_id=user.id, actor="system", action="account.created_from_channel",
                               data={"channel": ev.channel}))
        await session.flush()
    user = (await session.execute(select(User).where(User.id == link.user_id))).scalar_one()
    return user, link


async def issue_link_code(session: AsyncSession, user_id: uuid.UUID) -> str:
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    session.add(ChannelLinkCode(user_id=user_id, code_hash=hashlib.sha256(code.encode()).hexdigest(),
                                expires_at=datetime.now(UTC) + timedelta(minutes=10)))
    await session.flush()
    return code


async def link_channel(session: AsyncSession, link: LinkedChannel, code: str) -> uuid.UUID:
    """Move this channel identity onto the account that issued `code` (one-time, 10 minutes)."""
    row = (await session.execute(select(ChannelLinkCode).where(
        ChannelLinkCode.code_hash == hashlib.sha256(code.upper().encode()).hexdigest()).with_for_update()
    )).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        raise Forbidden("Link code is invalid or expired.")
    row.used_at = datetime.now(UTC)
    old_user = link.user_id
    link.user_id, link.verified_via = row.user_id, "link_code"
    session.add(AuditEvent(user_id=row.user_id, actor="user", action="channel.linked",
                           data={"channel": link.channel, "previous_user": str(old_user)}))
    await session.flush()
    return row.user_id


# --------------------------------------------------------------------------- conversation


async def current_conversation(session: AsyncSession, user_id: uuid.UUID) -> Conversation:
    conv = (await session.execute(select(Conversation).where(Conversation.user_id == user_id)
                                  .order_by(Conversation.created_at.desc()).limit(1))).scalar_one_or_none()
    if conv is None:
        conv = Conversation(user_id=user_id, title=None)
        session.add(conv)
        await session.flush()
    return conv


async def _open_clarification(session: AsyncSession, conv_id: uuid.UUID) -> tuple[Task, list[TaskStep]] | None:
    task = (await session.execute(select(Task).where(
        Task.conversation_id == conv_id, Task.status == TaskStatus.WAITING_FOR_USER).order_by(Task.created_at.desc())
    )).scalars().first()
    if task is None:
        return None
    asks = [s for s in task.steps if s.status == StepStatus.AWAITING_USER_ACTION and
            (s.tool == "ask_user" or (s.result or {}).get("question_key"))]
    return (task, asks) if asks else None


async def _pending_approvals(session: AsyncSession, user_id: uuid.UUID, conv_id: uuid.UUID) -> list[Approval]:
    return list((await session.execute(
        select(Approval).join(Task, Task.id == Approval.task_id).where(
            Approval.user_id == user_id, Approval.status == ApprovalStatus.PENDING, Task.conversation_id == conv_id)
    )).scalars())


_NOT_A_SLOT = {"unknown_intent", "unsupported_request"}


def _merge(steps: list[TaskStep], answer: str) -> str:
    parts: list[str] = []
    for s in steps:
        r = s.args if s.tool == "ask_user" else (s.result or {})
        p, clause = r.get("partial", {}) or {}, r.get("clause", "") or ""
        key = r.get("question_key")
        if key in ("train_origin_needed", "train_destination_needed") and not find_cities(answer):
            # The reply names no station/city, so it's not an answer ("movie ticket from bookmyshow"
            # must not become a train destination). Treat it as a new request.
            parts.append(answer)
        elif key == "train_origin_needed":
            parts.append(f"train from {answer} to {p.get('destination')} {clause}")
        elif key == "train_destination_needed":
            parts.append(f"train from {p.get('origin')} to {answer} {clause}")
        elif key == "offer_details_needed":
            parts.append(f"{offer_request(p, p.get('tool') or 'search_offers')} {answer}".strip())
        elif key in OFFER_QUESTIONS:
            # "1" / "sabse sasta" / "Flipkart wala" -> "book offer OFR-…" (planned deterministically);
            # an unrelated message is a new request on its own.
            parts.append(resolve_offer_answer(key, r, answer) or answer)
        elif key == "delivery_area_needed" and p.get("tool") == "order_shopping_list":
            parts.append(f"order my shopping list, deliver to {answer}")
        elif key == "time_needed" and p.get("tool") == "calendar_create_event":
            parts.append(f"add {p.get('text') or 'event'} to my calendar {clause} {answer}".strip())
        elif key in GROCERY_QUESTIONS and r.get("args"):
            clean = resolve_grocery_answer(key, r, answer)
            if key == "price_changed" and clean is None:
                parts.append("cancel my grocery order")
                continue
            parts.append(clean or f"{r.get('request') or clause}. Options offered: "
                         + "; ".join(f"{i}) {c}" for i, c in enumerate(r.get("choices") or [], 1))
                         + f". User's choice: {answer}")
        elif key == "weather_place_needed":
            parts.append(f"weather in {answer}")
        elif key == "delivery_area_needed":
            parts.append(f"{grocery_request({'items': p.get('items'), 'store': p.get('store')})} deliver to {answer}")
        elif key in CINEMA_QUESTIONS or key == "tickets_needed":
            # Re-plan the original request with the user's choice; the offered options are included so
            # "2" or "the 10:45 one" can be understood.
            if key in CINEMA_QUESTIONS and r.get("args") and (
                    clean := resolve_cinema_choice(key, r["args"], r.get("choices") or [], answer)):
                parts.append(clean)
                continue
            offered = "; ".join(f"{i}) {c}" for i, c in enumerate(r.get("choices") or [], 1))
            req = r.get("request") or clause or ""
            if key == "tickets_needed":
                req = (f"book tickets for {p.get('movie')} in {p.get('city')} {p.get('when_text') or ''} "
                       f"{('at ' + p['theatre']) if p.get('theatre') else ''}").strip()
            parts.append(f"{req}. " + (f"Options offered: {offered}. " if offered else "") + f"User's choice: {answer}")
        elif key == "movie_city_needed" and p.get("tool") == "book_movie_tickets":
            extra = " ".join(str(x) for x in (
                f"{p['tickets']} tickets" if p.get("tickets") else "", p.get("when_text") or "",
                f"at {p['theatre']}" if p.get("theatre") else "", p.get("show_time") or "",
                f"row {'/'.join(p['rows'])}" if p.get("rows") else "") if x)
            parts.append(f"book tickets for {p.get('movie')} in {answer} {extra}".strip())
        elif key == "movie_city_needed":
            bits = [f"{p.get('tickets')} tickets" if p.get("tickets") else "", p.get("when_text") or ""]
            parts.append(f"movie tickets for {p.get('movie') or clause} in {answer} {' '.join(bits)}".strip())
        elif key == "recipient_needed":
            parts.append(f"draft email to {answer}: {s.description}")
        elif key in _NOT_A_SLOT or key not in KNOWN_QUESTIONS:
            # "What can I help with?" / "I can't do that" are not slots to fill: the reply is a new
            # request on its own. Gluing the earlier clause on ("hi" + request) would leak it.
            parts.append(answer)
        elif p.get("tool") == "create_reminder" and p.get("text"):
            # Keep what to remind about across time questions ("pay electricity bill" was lost before).
            parts.append(f"remind me to {p['text']} {clause} {answer}".strip())
        else:
            parts.append(f"{clause} {answer}".strip())
    return " and ".join(parts)


class ConversationService:
    def __init__(self, temporal: Client) -> None:
        self.temporal = temporal

    async def handle_text(self, session: AsyncSession, user: User, channel: str, text: str,
                          dedupe_key: str | None = None) -> dict:
        conv = await current_conversation(session, user.id)
        # Card numbers, CVV, PINs, OTPs and passwords never reach storage or the planner.
        text, secrets_found = redact(text)
        lang = detect_language(text)
        msg = Message(conversation_id=conv.id, user_id=user.id, role="user", channel=channel, content=text[:8000],
                      language=lang.code, dedupe_key=dedupe_key)
        session.add(msg)
        try:
            await session.flush()
        except IntegrityError as exc:  # duplicate webhook delivery
            raise Conflict("Duplicate inbound message.") from exc
        if secrets_found:
            await self._system_reply(
                session, user.id, "🔒 Please don't send OTPs, PINs, CVVs, card numbers or passwords in chat. I removed "
                f"it ({', '.join(secrets_found)}) and won't use it. Payments are approved in your own UPI/bank app, "
                "and if a provider login needs a code I'll send a one-time secure link.", f"redacted:{msg.id}")
            if len(re.sub(r"\[[a-z ]+ removed\]|[^A-Za-zऀ-ॿ]", "", text)) < 6:
                await session.commit()
                return {"redacted": secrets_found, "task_id": None}
        await learn_from_text(session, text)  # BookMyShow links -> movie IDs for exact showtimes links

        # A short, exact affirmation/denial resolves a single pending approval (the card is on screen).
        pending = await _pending_approvals(session, user.id, conv.id)
        if len(pending) == 1 and (_AFFIRM.match(text) or _DENY.match(text)):
            decision = "APPROVED" if _AFFIRM.match(text) else "REJECTED"
            await self.decide(session, user.id, pending[0].id, decision,
                              evidence={"channel": channel, "message_id": str(msg.id), "text": text[:100]})
            return {"kind": "approval", "approval_id": str(pending[0].id), "decision": decision}

        goal = text
        clar = await _open_clarification(session, conv.id)
        if clar:
            old_task, asks = clar
            goal = _merge(asks, text)
            await self.temporal.get_workflow_handle(old_task.workflow_id).signal(TaskWorkflow.clarified)

        task = Task(user_id=user.id, conversation_id=conv.id, source_message_id=msg.id, goal=goal[:8000],
                    language=lang.code, status=TaskStatus.CREATED)
        session.add(task)
        await session.flush()
        task.workflow_id = f"task-{task.id}"
        msg.task_id = task.id
        await session.commit()  # the workflow's first activity must see the row
        s = get_settings()
        await self.temporal.start_workflow(
            TaskWorkflow.run, args=[str(task.id), s.approval_ttl_seconds, s.clarification_ttl_seconds],
            id=task.workflow_id, task_queue=DEFAULT_QUEUE)
        return {"kind": "task", "task_id": str(task.id), "message_id": str(msg.id)}

    async def decide(self, session: AsyncSession, user_id: uuid.UUID, approval_id: uuid.UUID, decision: str,
                     evidence: dict) -> Approval:
        appr = (await session.execute(select(Approval).where(Approval.id == approval_id).with_for_update())
                ).scalar_one_or_none()
        if appr is None or appr.user_id != user_id:
            raise NotFound("Approval not found.")  # never reveal other users' approvals
        if appr.status != ApprovalStatus.PENDING:
            raise Conflict(f"Approval already {appr.status}.")
        if appr.expires_at <= datetime.now(UTC):
            appr.status = ApprovalStatus.EXPIRED
            raise Conflict("Approval expired.")
        appr.status = ApprovalStatus(decision)
        appr.decided_at = datetime.now(UTC)
        appr.decision_evidence = evidence
        step = (await session.execute(select(TaskStep).where(TaskStep.id == appr.step_id))).scalar_one()
        task = (await session.execute(select(Task).where(Task.id == appr.task_id))).scalar_one()
        session.add(AuditEvent(user_id=user_id, actor="user", action=f"approval.{decision.lower()}",
                               target_type="approval", target_id=str(appr.id), data=evidence))
        await session.commit()  # decision is durable before the workflow acts on it
        await self.temporal.get_workflow_handle(task.workflow_id).signal(
            TaskWorkflow.approval_decided, args=[step.step_key, decision])
        return appr

    async def reminder_button(self, session: AsyncSession, user_id: uuid.UUID, verb: str,
                              reminder_id: uuid.UUID) -> dict:
        """Reminder follow-up buttons: Done stops the nudge; Snooze reminds again in 10 minutes (max 3)."""
        from zoneinfo import ZoneInfo

        r = (await session.execute(select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)
                                   .with_for_update())).scalar_one_or_none()
        if r is None:
            raise NotFound("Reminder not found.")  # never act on another user's reminder
        now = datetime.now(UTC)
        tag = f"{r.id}:{r.snoozes}:{'done' if r.done_at else 'open'}"
        if verb == "rdone" or r.done_at is not None:
            if r.done_at is None:
                r.done_at = now
                session.add(AuditEvent(user_id=user_id, actor="user", action="reminder.done", target_type="reminder",
                                       target_id=str(r.id), data={}))
            await self._system_reply(session, user_id, f"✅ Done: {r.text}", f"rbtn-done:{tag}")
            await session.commit()
            for wid in (r.workflow_id, f"{r.workflow_id}-s{r.snoozes}"):
                try:
                    await self.temporal.get_workflow_handle(wid).signal(ReminderWorkflow.done)
                except Exception:  # noqa: BLE001 - already finished or never existed: nothing to stop
                    pass
            return {"kind": "reminder_done"}
        if r.snoozes >= 3:
            await self._system_reply(session, user_id, "⏰ Snoozed 3 times already. Tell me a new time if you still "
                                                       f"want a reminder: {r.text}", f"rbtn-max:{tag}")
            await session.commit()
            return {"kind": "reminder_snooze_limit"}
        r.snoozes += 1
        fire = now + timedelta(minutes=10)
        r.fire_at_utc, r.status = fire, "SCHEDULED"
        local = fire.astimezone(ZoneInfo(r.timezone))
        r.local_display = local.strftime("%a %d %b %Y, %I:%M %p") + f" ({r.timezone})"
        await self._system_reply(session, user_id, f"⏰ Okay, I'll remind you again at "
                                                   f"{local.strftime('%I:%M %p').lstrip('0')}: {r.text}",
                                 f"rbtn-snooze:{tag}")
        await session.commit()
        await self.temporal.start_workflow(
            ReminderWorkflow.run, args=[str(r.id), fire.isoformat(), get_settings().reminder_followup_seconds],
            id=f"{r.workflow_id or f'reminder-{r.id}'}-s{r.snoozes}", task_queue=DEFAULT_QUEUE)
        return {"kind": "reminder_snoozed", "fire_at": fire.isoformat()}

    async def _system_reply(self, session: AsyncSession, user_id: uuid.UUID, text: str, dedupe: str) -> None:
        from apna.channels.outbound import deliver_to_user

        conv = await current_conversation(session, user_id)
        msg = Message(conversation_id=conv.id, user_id=user_id, role="assistant", channel="system", content=text,
                      payload={}, dedupe_key=dedupe)
        session.add(msg)
        await session.flush()
        await deliver_to_user(session, user_id, msg)

    async def handle_inbound(self, session: AsyncSession, ev: InboundEvent) -> dict:
        if ev.kind == "status":
            row = (await session.execute(select(OutboundMessage).where(
                OutboundMessage.provider_message_id == ev.provider_message_id))).scalar_one_or_none()
            if row and ev.status:
                row.status = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ", "failed": "FAILED"}.get(
                    ev.status, row.status)
                if ev.errors:
                    row.error = str(ev.errors)[:500]
            # Also covers messages we did not send (e.g. Meta dashboard test templates): ids and codes only.
            _log.info("channel_status", channel=str(ev.channel), status=ev.status, ours=row is not None,
                      error_codes=[e.get("code") for e in (ev.errors or []) if isinstance(e, dict)])
            return {"kind": "status"}
        user, link = await resolve_channel_user(session, ev)
        link.last_inbound_at = ev.timestamp or datetime.now(UTC)
        if ev.kind == "button" and ev.button_id:
            verb, _, ident = ev.button_id.partition(":")
            if verb in {"approve", "reject"}:
                await self.decide(session, user.id, uuid.UUID(ident), "APPROVED" if verb == "approve" else "REJECTED",
                                  evidence={"channel": ev.channel, "provider_message_id": ev.provider_message_id})
                return {"kind": "approval", "decision": verb}
            if verb in {"rdone", "rsnooze"}:
                return await self.reminder_button(session, user.id, verb, uuid.UUID(ident))
        if ev.kind == "unsupported":
            return {"kind": "unsupported"}
        if ev.text.strip().lower() in {"/linkcode", "linkcode"}:
            # Issue a one-time code on this (provider-verified) chat; typing `link CODE` in another
            # app's chat within 10 minutes attaches that chat to this same account.
            code = await issue_link_code(session, user.id)
            await self._system_reply(session, user.id, f"🔗 Your link code: {code}\nSend \"link {code}\" from your "
                                                       "other chat app within 10 minutes. Never share it with anyone.",
                                     f"linkcode:{ev.channel}:{ev.provider_message_id}")
            return {"kind": "link_code"}
        if m := _LINK.match(ev.text):
            await link_channel(session, link, m.group(1))
            await self._system_reply(session, link.user_id, "✅ Linked. This chat now shares your assistant, tasks "
                                                            "and history.", f"linked:{ev.provider_message_id}")
            return {"kind": "linked"}
        return await self.handle_text(session, user, ev.channel, ev.text,
                                      dedupe_key=f"{ev.channel}:{ev.provider_message_id}")
