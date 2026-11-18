"""Temporal activities: the only code that writes task projections and performs side effects."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy

from apna.agent import policy
from apna.agent.assist import direct_plan, prepare_offer_payment
from apna.agent.commerce import prepare_grocery_payment
from apna.agent.executors import (
    EXECUTORS,
    IDEMPOTENT_SIDE_EFFECTS,
    ExecResult,
    ToolContext,
    default_providers,
    prepare_movie_payment,
    prepare_send_email,
)
from apna.agent.grounding import ground_plan
from apna.agent.language import detect_language
from apna.agent.planner import PlannerInput, PlannerUnavailable, RulePlanner, build_planner
from apna.agent.replies import render_task_reply
from apna.agent.tools import get_tool, substitute_refs
from apna.core.config import get_settings
from apna.core.errors import ApnaError, ApprovalRequired, PolicyViolation, UserHandoffRequired
from apna.core.logging import log
from apna.db.models import (
    Approval,
    AuditEvent,
    Message,
    Notification,
    Reminder,
    Task,
    TaskStep,
    ToolExecution,
    UserProfile,
    WorkflowEvent,
)
from apna.db.session import session_scope
from apna.domain.enums import TASK_TRANSITIONS, ApprovalStatus, StepStatus, TaskStatus
from apna.workflows.definitions import DEFAULT_QUEUE, PaymentWorkflow, PriceWatchWorkflow, ReminderWorkflow

_client: Client | None = None


def set_temporal_client(client: Client) -> None:
    global _client
    _client = client


# --------------------------------------------------------------------------- helpers


async def _event(session: AsyncSession, task_id: uuid.UUID, kind: str, data: dict[str, Any],
                 dedupe: str | None = None) -> None:
    if dedupe and (await session.execute(select(WorkflowEvent.id).where(WorkflowEvent.dedupe_key == dedupe))).first():
        return
    seq = (await session.execute(
        select(func.coalesce(func.max(WorkflowEvent.seq), 0)).where(WorkflowEvent.task_id == task_id)
    )).scalar_one() + 1
    session.add(WorkflowEvent(task_id=task_id, seq=seq, kind=kind, data=data, dedupe_key=dedupe))


async def _task(session: AsyncSession, task_id: str, lock: bool = False) -> Task:
    q = select(Task).where(Task.id == uuid.UUID(task_id))
    if lock:
        q = q.with_for_update()
    return (await session.execute(q)).scalar_one()


async def _step(session: AsyncSession, task_id: str, key: str) -> TaskStep:
    return (await session.execute(
        select(TaskStep).where(TaskStep.task_id == uuid.UUID(task_id), TaskStep.step_key == key).with_for_update()
    )).scalar_one()


async def _transition(session: AsyncSession, task: Task, new: TaskStatus) -> None:
    cur = TaskStatus(task.status)
    if cur == new:
        return
    if new not in TASK_TRANSITIONS.get(cur, set()):
        raise PolicyViolation(f"Illegal task transition {cur} -> {new}")
    task.status = new
    await _event(session, task.id, "task_status", {"from": cur, "to": new})


async def _profile(session: AsyncSession, user_id: uuid.UUID) -> UserProfile | None:
    return (await session.execute(select(UserProfile).where(UserProfile.user_id == user_id))).scalar_one_or_none()


async def _start_reminder(reminder_id: str, fire_at_utc: datetime) -> str:
    assert _client is not None, "Temporal client not configured in worker"
    handle = await _client.start_workflow(
        ReminderWorkflow.run, args=[reminder_id, fire_at_utc.isoformat(), get_settings().reminder_followup_seconds],
        id=f"reminder-{reminder_id}",
        task_queue=DEFAULT_QUEUE, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    desc = await handle.describe()  # verify with the scheduler itself, not our own optimism
    return "RUNNING" if desc.status == WorkflowExecutionStatus.RUNNING else str(desc.status)


async def _start_payment(intent_id: str) -> str:
    assert _client is not None, "Temporal client not configured in worker"
    from apna.db.models import PaymentIntent

    async with session_scope() as session:
        intent = await session.get(PaymentIntent, uuid.UUID(intent_id))
        expires = (intent.expires_at if intent and intent.expires_at else datetime.now(UTC) + timedelta(minutes=20))
    handle = await _client.start_workflow(
        PaymentWorkflow.run, args=[intent_id, expires.isoformat(), get_settings().payment_poll_seconds],
        id=f"payment-{intent_id}",
        task_queue=DEFAULT_QUEUE, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    return handle.id


async def _providers(session: AsyncSession, user_id: uuid.UUID) -> tuple[Any, Any]:
    """Gmail when the user connected Google (and keys are configured), else the sandbox mailbox."""
    email, rail = default_providers()
    s = get_settings()
    if s.google_configured():
        from apna.providers import google

        if await google.connected(session, user_id):
            email = google.GmailProvider(s, session, user_id)
    return email, rail


async def _start_price_watch(watch_id: str) -> str:
    assert _client is not None, "Temporal client not configured in worker"
    handle = await _client.start_workflow(
        PriceWatchWorkflow.run, args=[watch_id, get_settings().price_watch_poll_seconds], id=f"pricewatch-{watch_id}",
        task_queue=DEFAULT_QUEUE, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    return handle.id


# --------------------------------------------------------------------------- planning


@activity.defn(name="plan_task")
async def plan_task(task_id: str) -> dict[str, Any]:
    settings = get_settings()
    async with session_scope() as session:
        task = await _task(session, task_id, lock=True)
        if task.steps:  # idempotent on retry / replay
            return {"steps": [_step_ref(s) for s in task.steps]}
        await _transition(session, task, TaskStatus.PLANNING)
        profile = await _profile(session, task.user_id)
        tz = profile.timezone if profile else settings.default_timezone
        lang = detect_language(task.goal)
        history_rows = (await session.execute(
            select(Message).where(Message.conversation_id == task.conversation_id)
            .order_by(Message.created_at.desc()).limit(7)
        )).scalars().all()
        history = [{"role": m.role, "content": m.content} for m in reversed(history_rows)
                   if m.id != task.source_message_id]
        now = datetime.now(UTC)
        planner = build_planner(settings)
        inp = PlannerInput(task.goal, lang, tz, now.astimezone(ZoneInfo(tz)).isoformat(), history)
        try:
            # Deterministic plans first: picking an offer from a list must not depend on a model.
            direct = direct_plan(task.goal)
            try:
                draft = direct if direct is not None else await planner.plan(inp)
            except PlannerUnavailable as exc:
                # Provider down / out of credit / bad key: answer with the offline planner instead of
                # retrying a paid call forever. Grounding and policy still apply unchanged.
                log.warning("planner_fallback", task_id=str(task.id), planner=planner.name, error=exc.message)
                await _event(session, task.id, "planner_fallback", {"from": planner.name, "error": exc.message})
                planner = RulePlanner()
                draft = await planner.plan(inp)
            plan = ground_plan(draft, planner="direct-v1" if direct is not None else planner.name,
                               language=lang, tz=tz, now=now,
                             browser_enabled=settings.browser_enabled)
        except ApnaError as exc:
            task.status = TaskStatus.FAILED
            task.error = f"{exc.code}: {exc.message}"
            await _event(session, task.id, "planning_failed", {"error": task.error})
            return {"failed": True, "steps": []}
        task.plan = plan.model_dump(mode="json")
        task.planner = plan.planner
        task.language = lang.code
        for i, s in enumerate(plan.steps):
            session.add(TaskStep(task_id=task.id, step_key=s.key, position=i, tool=s.tool,
                                 description=s.description, args=s.args, depends_on=s.depends_on,
                                 risk_level=s.risk_level, status=StepStatus.PLANNED))
        await _transition(session, task, TaskStatus.READY)
        await _event(session, task.id, "planned", {"planner": planner.name, "steps": len(plan.steps)})
        await session.flush()
        await session.refresh(task, ["steps"])
        return {"steps": [_step_ref(s) for s in task.steps]}


def _step_ref(s: TaskStep) -> dict[str, Any]:
    return {"key": s.step_key, "tool": s.tool, "depends_on": list(s.depends_on),
            "task_queue": get_tool(s.tool).task_queue}


@activity.defn(name="set_task_status")
async def set_task_status(task_id: str, status: str) -> dict[str, Any]:
    async with session_scope() as session:
        task = await _task(session, task_id, lock=True)
        target = TaskStatus(status)
        if TaskStatus(task.status) == TaskStatus.READY and target != TaskStatus.RUNNING:
            await _transition(session, task, TaskStatus.RUNNING)
        await _transition(session, task, target)
        return {"status": task.status}


# --------------------------------------------------------------------------- steps


@activity.defn(name="prepare_step")
async def prepare_step(task_id: str, key: str) -> dict[str, Any]:
    """Resolve references, evaluate policy, create the exact-action approval when needed."""
    settings = get_settings()
    async with session_scope() as session:
        task = await _task(session, task_id)
        step = await _step(session, task_id, key)
        if StepStatus(step.status) != StepStatus.PLANNED:
            if step.status == StepStatus.AWAITING_APPROVAL:
                return {"action": "await_approval"}
            return {"action": "settled", "status": step.status}
        spec = get_tool(step.tool)
        results = {s.step_key: s.result for s in task.steps}
        try:
            args = substitute_refs(step.args, results)
        except KeyError:
            # The earlier step produced nothing to act on (no cart, offer gone): nothing to pay for.
            step.status, step.error = StepStatus.SKIPPED, "Nothing to act on from the earlier step"
            return {"action": "settled", "status": step.status}
        try:
            email, rail = await _providers(session, task.user_id)
            ctx = _ctx(session, task, step, spec, await _profile(session, task.user_id), email, rail)
            if step.tool == "send_email":
                args = await prepare_send_email(ctx, args)
            if step.tool == "pay_movie_booking":
                args = await prepare_movie_payment(ctx, args)
            if step.tool == "pay_grocery_order":
                args = await prepare_grocery_payment(ctx, args)
            if step.tool == "pay_offer":
                args = await prepare_offer_payment(ctx, args)
            if step.tool == "browser_submit_form" and not args.get("fields"):
                args = {**args, "fields": await _profile_form_fields(session, task, ctx.profile)}
            spec.args_model.model_validate(args)
        except (KeyError, ValueError) as exc:
            step.status, step.error = StepStatus.FAILED, f"Could not prepare step: {exc}"
            return {"action": "settled", "status": step.status}
        step.args = args
        decision = policy.evaluate(spec, args, ctx.profile)
        if not decision.allowed:
            step.status, step.error = StepStatus.FAILED, f"Blocked by policy: {decision.reason}"
            await _event(session, task.id, "policy_denied", {"step": key, "reason": decision.reason})
            return {"action": "settled", "status": step.status}
        if not decision.requires_approval:
            return {"action": "execute"}

        approval = (await session.execute(select(Approval).where(Approval.step_id == step.id))).scalar_one_or_none()
        if approval is None:
            approval = Approval(
                user_id=task.user_id, task_id=task.id, step_id=step.id, tool=spec.name,
                operation_summary=_summary(spec.name, args), action_hash=policy.action_hash(spec.name, args),
                action_snapshot=args, target_provider=spec.integration,
                recipient=args.get("to_address"), risk_level=int(spec.risk), status=ApprovalStatus.PENDING,
                policy_version=policy.POLICY_VERSION,
                expires_at=datetime.now(UTC) + timedelta(seconds=settings.approval_ttl_seconds),
            )
            session.add(approval)
            await session.flush()
        step.status = StepStatus.AWAITING_APPROVAL
        step.result = {"approval_id": str(approval.id), **_preview(spec.name, args)}
        await _event(session, task.id, "approval_requested", {"step": key, "approval_id": str(approval.id)})
        return {"action": "await_approval", "approval_id": str(approval.id)}


async def _profile_form_fields(session: AsyncSession, task: Task, profile: UserProfile | None) -> dict[str, str]:
    """Fill the sandbox form only from data the user has given us (profile), never invented."""
    from apna.db.models import User

    user = (await session.execute(select(User).where(User.id == task.user_id))).scalar_one()
    if not profile or not user.email:
        raise ValueError("Your name and email are needed to fill this form.")
    return {"full_name": profile.display_name, "email": user.email, "preferred_day": "Saturday"}


def _summary(tool: str, args: dict[str, Any]) -> str:
    if tool == "send_email":
        return f"Send email to {args['to_address']} with subject '{args['subject']}'"
    if tool == "browser_submit_form":
        return f"Submit form on {args['site_key']} with fields {sorted(args['fields'])}"
    if tool == "pay_grocery_order":
        return (f"TEST payment of {args['amount_minor'] / 100:.2f} {args['currency']} to {args['merchant']} for "
                f"{len(args['items'])} items")
    if tool == "pay_offer":
        if args["payment_method"] == "none":
            return f"Confirm free TEST reservation: {args['title']} ({args['merchant']})"
        return (f"Payment of {args['amount_minor'] / 100:.2f} {args['currency']} to {args['merchant']} for "
                f"{args['title']}")
    if tool == "calendar_create_event":
        return f"Add '{args['title']}' to Google Calendar at {args['start_local']}"
    if tool == "pay_movie_booking":
        return (f"TEST payment of {args['amount_minor'] / 100:.2f} {args['currency']} to {args['merchant']} for "
                f"{args['movie']} at {args['theatre']}, seats {', '.join(args['seats'])}")
    return f"{tool} {args}"


def _preview(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool == "send_email":
        return {"to_address": args["to_address"], "subject": args["subject"]}
    if tool == "browser_submit_form":
        from apna.browser.executor import SITES

        site = SITES.get(args["site_key"])
        return {"site": site.label if site else args["site_key"],
                "fields_preview": ", ".join(f"{k}={v}" for k, v in args["fields"].items())}
    if tool == "pay_grocery_order":
        return {k: args.get(k) for k in ("amount_minor", "store", "items", "deliver_to", "payment_method")}
    if tool == "pay_offer":
        return {k: args.get(k) for k in ("amount_minor", "title", "merchant", "when", "vertical", "payment_method")}
    if tool == "calendar_create_event":
        return {k: args.get(k) for k in ("title", "start_local", "timezone", "duration_min")}
    if tool == "pay_movie_booking":
        return {k: args[k] for k in ("amount_minor", "movie", "theatre", "starts_at", "seats", "seat_class")}
    return {}


def _ctx(session: AsyncSession, task: Task, step: TaskStep, spec: Any, profile: UserProfile | None,
         email: Any, rail: Any) -> ToolContext:
    return ToolContext(
        session=session, settings=get_settings(), user_id=task.user_id, task_id=task.id, step_id=step.id,
        step_key=step.step_key, idempotency_key=f"{task.id}:{step.step_key}", source_text=task.goal,
        profile=profile, spec=spec, email=email, rail=rail, start_reminder_workflow=_start_reminder,
        start_payment_workflow=_start_payment, start_price_watch_workflow=_start_price_watch,
    )


async def _begin_execution(session: AsyncSession, task: Task, step: TaskStep) -> ToolExecution | dict[str, Any]:
    """Idempotency gate (ADR-005). Returns a stored outcome or a fresh execution row."""
    key = f"{task.id}:{step.step_key}"
    exe = (await session.execute(
        select(ToolExecution).where(ToolExecution.idempotency_key == key).with_for_update()
    )).scalar_one_or_none()
    if exe and exe.status == "SUCCEEDED":
        return {"status": step.status}
    if exe and exe.status == "STARTED" and step.tool not in IDEMPOTENT_SIDE_EFFECTS:
        # A previous attempt may have acted externally before crashing: never repeat blindly.
        step.status = StepStatus.AWAITING_USER_ACTION
        step.result = {**(step.result or {}), "outcome_uncertain": True}
        step.error = "A previous attempt was interrupted; outcome must be checked before retrying."
        return {"status": step.status}
    if exe is None:
        exe = ToolExecution(user_id=task.user_id, task_id=task.id, step_id=step.id, idempotency_key=key,
                            tool=step.tool, status="STARTED")
        session.add(exe)
    else:
        exe.attempt += 1
        exe.status = "STARTED"
    step.status = StepStatus.STARTED
    await session.commit()  # persist STARTED before any side effect
    return exe


async def _finish_execution(session: AsyncSession, task: Task, step: TaskStep, exe: ToolExecution,
                            res: ExecResult) -> dict[str, Any]:
    step.status = res.status
    if res.result:
        step.result = {**({"approval_id": step.result.get("approval_id")} if step.result.get("approval_id") else {}),
                       **res.result}
    step.error = res.error
    exe.status = "FAILED" if res.status == StepStatus.FAILED else "SUCCEEDED"
    exe.result = res.result
    exe.error = res.error
    exe.finished_at = datetime.now(UTC)
    await _event(session, task.id, "step_finished", {"step": step.step_key, "status": res.status})
    session.add(AuditEvent(user_id=task.user_id, actor="agent", action=f"tool.{step.tool}", target_type="task_step",
                           target_id=str(step.id), data={"status": res.status, "error": res.error}))
    return {"status": step.status}


@activity.defn(name="execute_step")
async def execute_step(task_id: str, key: str) -> dict[str, Any]:
    async with session_scope() as session:
        task = await _task(session, task_id)
        step = await _step(session, task_id, key)
        gate = await _begin_execution(session, task, step)
        if isinstance(gate, dict):
            return gate
        spec = get_tool(step.tool)
        email, rail = await _providers(session, task.user_id)
        ctx = _ctx(session, task, step, spec, await _profile(session, task.user_id), email, rail)
        try:
            res = await EXECUTORS[step.tool](ctx, step.args)
        except (ApprovalRequired, PolicyViolation) as exc:
            res = ExecResult(StepStatus.FAILED, error=f"{exc.code}: {exc.message}")
        except UserHandoffRequired as exc:
            res = ExecResult(StepStatus.AWAITING_USER_ACTION, {"handoff": True}, exc.message)
        return await _finish_execution(session, task, step, gate, res)


@activity.defn(name="execute_browser_step")
async def execute_browser_step(task_id: str, key: str) -> dict[str, Any]:
    from apna.browser.executor import SITES, BrowserRun, run_read_page, run_submit_form

    settings = get_settings()
    async with session_scope() as session:
        task = await _task(session, task_id)
        step = await _step(session, task_id, key)
        gate = await _begin_execution(session, task, step)
        if isinstance(gate, dict):
            return gate
        spec = get_tool(step.tool)
        profile = await _profile(session, task.user_id)
        site = SITES.get(step.args.get("site_key", ""))
        try:
            if site is None:
                raise PolicyViolation("Unknown or non-approved site.")
            async with BrowserRun(session, settings, task.user_id, task.id) as run:
                if step.tool == "browser_read_page":
                    data = await run_read_page(run, site)
                    res = ExecResult(StepStatus.COMPLETED_LOCALLY, data)
                else:
                    await policy.assert_authorized(session, step_id=step.id, spec=spec, args=step.args,
                                                   profile=profile)
                    data = await run_submit_form(run, site, step.args["fields"])
                    res = ExecResult(StepStatus.CONFIRMED_BY_PROVIDER if data["verified_with_site"]
                                     else StepStatus.SUBMITTED_EXTERNALLY, data)
        except UserHandoffRequired as exc:
            res = ExecResult(StepStatus.AWAITING_USER_ACTION, {"handoff": True, "site": site.label if site else ""},
                             exc.message)
        except (ApprovalRequired, PolicyViolation) as exc:
            res = ExecResult(StepStatus.FAILED, error=f"{exc.code}: {exc.message}")
        return await _finish_execution(session, task, step, gate, res)


@activity.defn(name="settle_step")
async def settle_step(task_id: str, key: str, status: str, reason: str) -> dict[str, Any]:
    async with session_scope() as session:
        step = await _step(session, task_id, key)
        if not StepStatus(step.status).is_success:
            step.status, step.error = StepStatus(status), reason
        await _event(session, uuid.UUID(task_id), "step_settled", {"step": key, "status": status})
        return {"status": step.status}


@activity.defn(name="settle_step_many")
async def settle_step_many(task_id: str, keys: list[str], status: str, reason: str) -> dict[str, Any]:
    for k in keys:
        await settle_step(task_id, k, status, reason)
    return {"count": len(keys)}


@activity.defn(name="expire_waiting")
async def expire_waiting(task_id: str, keys: list[str]) -> dict[str, Any]:
    async with session_scope() as session:
        for k in keys:
            step = await _step(session, task_id, k)
            step.status = StepStatus.CANCELLED
            step.result = {**(step.result or {}), "expired": True}
            appr = (await session.execute(select(Approval).where(Approval.step_id == step.id))).scalar_one_or_none()
            if appr and appr.status == ApprovalStatus.PENDING:
                appr.status = ApprovalStatus.EXPIRED
        await _event(session, uuid.UUID(task_id), "expired", {"steps": keys})
        return {"count": len(keys)}


@activity.defn(name="cancel_task")
async def cancel_task(task_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        task = await _task(session, task_id, lock=True)
        for s in task.steps:
            if not StepStatus(s.status).is_terminal:
                s.status = StepStatus.CANCELLED
        for a in (await session.execute(select(Approval).where(Approval.task_id == task.id,
                                                               Approval.status == ApprovalStatus.PENDING))).scalars():
            a.status = ApprovalStatus.REVOKED
        task.status = TaskStatus.CANCELLED
        await _event(session, task.id, "cancelled", {})
        return {"status": task.status}


# --------------------------------------------------------------------------- replies


SUPERSEDED = "Superseded by the user's follow-up message"


def _overall(steps: list[TaskStep]) -> TaskStatus:
    # Clarification steps answered by a follow-up task are neutral, not failures.
    live = [s for s in steps if not (s.status == StepStatus.SKIPPED and s.error == SUPERSEDED)]
    if not live:
        return TaskStatus.CANCELLED
    sts = [StepStatus(s.status) for s in live]
    if all(s.is_success or s in {StepStatus.REJECTED} for s in sts):
        return TaskStatus.COMPLETED
    if any(s == StepStatus.AWAITING_USER_ACTION for s in sts) or any(s.is_success for s in sts) or \
            any(s == StepStatus.SUBMITTED_EXTERNALLY for s in sts):
        return TaskStatus.PARTIALLY_COMPLETED
    if all(s == StepStatus.CANCELLED for s in sts):
        return TaskStatus.EXPIRED
    return TaskStatus.FAILED


async def _publish(session: AsyncSession, task: Task, dedupe: str) -> Message | None:
    if (await session.execute(select(Message.id).where(Message.dedupe_key == dedupe))).first():
        return None
    steps = [{"key": s.step_key, "tool": s.tool, "description": s.description, "status": s.status,
              "result": s.result, "error": s.error, "approval_id": (s.result or {}).get("approval_id")}
             for s in task.steps if s.error != SUPERSEDED]
    text, actions = render_task_reply(task.language or "en", steps)
    if not text and task.error:
        text = f"❌ {task.error}"
    if not text:
        return None  # nothing new to tell the user (e.g. fully superseded clarification)
    msg = Message(conversation_id=task.conversation_id, user_id=task.user_id, role="assistant", channel="system",
                  content=text or "✅", language=task.language, task_id=task.id,
                  payload={"actions": actions, "task_status": task.status}, dedupe_key=dedupe)
    session.add(msg)
    await session.flush()
    from apna.channels.outbound import deliver_to_user

    await deliver_to_user(session, task.user_id, msg)
    return msg


@activity.defn(name="publish_reply")
async def publish_reply(task_id: str, round_no: int) -> dict[str, Any]:
    async with session_scope() as session:
        task = await _task(session, task_id)
        msg = await _publish(session, task, f"task:{task_id}:round:{round_no}")
        return {"message_id": str(msg.id) if msg else None}


@activity.defn(name="finalize_task")
async def finalize_task(task_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        task = await _task(session, task_id, lock=True)
        if TaskStatus(task.status) not in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            final = _overall(task.steps) if task.steps else TaskStatus.FAILED
            if TaskStatus(task.status) != TaskStatus.VERIFYING:
                if TaskStatus(task.status) == TaskStatus.READY:
                    await _transition(session, task, TaskStatus.RUNNING)
                await _transition(session, task, TaskStatus.VERIFYING)
            await _transition(session, task, final)
        task.result_summary = {s.step_key: s.status for s in task.steps}
        if any(s.error == SUPERSEDED for s in task.steps):
            # The user moved on (their reply became a new task, which answers for itself). Re-sending
            # this task's earlier results would repeat old messages.
            log.info("task_finalized", task_id=task_id, status=task.status, published=False)
            return {"status": task.status}
        await _publish(session, task, f"task:{task_id}:final")
        log.info("task_finalized", task_id=task_id, status=task.status)
        return {"status": task.status}


# --------------------------------------------------------------------------- reminders


@activity.defn(name="deliver_reminder")
async def deliver_reminder(reminder_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        r = (await session.execute(select(Reminder).where(Reminder.id == uuid.UUID(reminder_id))
                                   .with_for_update())).scalar_one()
        if r.status == "DELIVERED":
            return {"status": "DELIVERED", "duplicate": True, "snoozes": r.snoozes}
        suffix = f":{r.snoozes}" if r.snoozes else ""
        dedupe = f"reminder:{r.id}{suffix}"
        exists = (await session.execute(select(Notification.id).where(Notification.dedupe_key == dedupe))).first()
        if not exists:
            session.add(Notification(user_id=r.user_id, kind="reminder", title="⏰ Reminder", body=r.text,
                                     data={"reminder_id": str(r.id)}, dedupe_key=dedupe))
        r.status, r.delivered_at = "DELIVERED", datetime.now(UTC)
        conv_id = None
        if r.task_id:
            conv_id = (await session.execute(select(Task.conversation_id).where(Task.id == r.task_id))).scalar_one()
        if conv_id:
            msg = Message(conversation_id=conv_id, user_id=r.user_id, role="assistant", channel="system",
                          content=f"⏰ {r.text}", task_id=r.task_id,
                          payload={"kind": "reminder", "actions": reminder_buttons(r.id)},
                          dedupe_key=f"reminder-msg:{r.id}{suffix}")
            session.add(msg)
            await session.flush()
            from apna.channels.outbound import deliver_to_user

            await deliver_to_user(session, r.user_id, msg, proactive=True)
        return {"status": "DELIVERED", "snoozes": r.snoozes}


def reminder_buttons(reminder_id: uuid.UUID) -> list[dict[str, Any]]:
    return [{"type": "buttons", "buttons": [[f"rdone:{reminder_id}", "✅ Done"],
                                            [f"rsnooze:{reminder_id}", "⏰ Snooze 10 min"]]}]


@activity.defn(name="reminder_followup")
async def reminder_followup(reminder_id: str, snoozes_at_delivery: int) -> dict[str, Any]:
    """One gentle nudge if the user hasn't marked it done (and hasn't snoozed since)."""
    async with session_scope() as session:
        r = (await session.execute(select(Reminder).where(Reminder.id == uuid.UUID(reminder_id)))).scalar_one()
        if r.done_at is not None or r.snoozes != snoozes_at_delivery or r.status != "DELIVERED":
            return {"sent": False}
        await _notify(session, r.user_id, f"👋 Following up: did you get to this? ⏰ {r.text}",
                      f"reminder-followup:{r.id}:{r.snoozes}", actions=reminder_buttons(r.id), kind="reminder")
        return {"sent": True}


@activity.defn(name="check_price_watch")
async def check_price_watch(watch_id: str) -> dict[str, Any]:
    from apna.agent.assist import check_watch
    from apna.db.models import PriceWatch

    async with session_scope() as session:
        out = await check_watch(session, uuid.UUID(watch_id))
        if out["notify"]:
            w = await session.get(PriceWatch, uuid.UUID(watch_id))
            assert w is not None
            await _notify(session, w.user_id, out["notify"], f"pricewatch:{watch_id}:{w.status}", kind="price_watch")
        return {"done": out["done"]}


@activity.defn(name="mark_reminder")
async def mark_reminder(reminder_id: str, status: str) -> dict[str, Any]:
    async with session_scope() as session:
        r = (await session.execute(select(Reminder).where(Reminder.id == uuid.UUID(reminder_id)))).scalar_one()
        r.status = status
        return {"status": status}


async def _notify(session: AsyncSession, user_id: uuid.UUID, text: str, dedupe: str, *,
                  actions: list[dict[str, Any]] | None = None, kind: str = "payment") -> None:
    """Proactive message in the user's latest conversation (deduplicated)."""
    from apna.channels.outbound import deliver_to_user
    from apna.db.models import Conversation

    exists = (await session.execute(select(Message.id).where(Message.dedupe_key == dedupe))).first()
    if exists:
        return
    conv = (await session.execute(select(Conversation).where(Conversation.user_id == user_id)
                                  .order_by(Conversation.created_at.desc()))).scalars().first()
    if conv is None:
        return
    msg = Message(conversation_id=conv.id, user_id=user_id, role="assistant", channel="system", content=text,
                  payload={"kind": kind, **({"actions": actions} if actions else {})}, dedupe_key=dedupe)
    session.add(msg)
    await session.flush()
    await deliver_to_user(session, user_id, msg, proactive=True)


@activity.defn(name="reconcile_payment")
async def reconcile_payment(intent_id: str) -> str:
    """Ask the gateway for the truth (status API). Never trusts our own optimism."""
    from apna.db.models import PaymentIntent
    from apna.providers import payments

    async with session_scope() as session:
        intent = (await session.execute(select(PaymentIntent).where(PaymentIntent.id == uuid.UUID(intent_id))
                                        .with_for_update())).scalar_one()
        if intent.status in payments.TERMINAL or intent.gateway != "razorpay" or not intent.gateway_ref:
            return intent.status
        link = await payments.RazorpayGateway(get_settings()).fetch_link(intent.gateway_ref)
        paid_ids = [p.get("payment_id") for p in (link.get("payments") or []) if p.get("status") == "captured"]
        payments.apply_status(intent, payments.status_from_link(link),
                              {"link_status": link.get("status"), "payment_ids": paid_ids})
        return intent.status


@activity.defn(name="finish_payment")
async def finish_payment(intent_id: str, status: str) -> dict[str, Any]:
    """Complete the purchase once if PAID; otherwise release it and tell the user nothing was ordered."""
    from apna.db.models import PaymentIntent
    from apna.providers import grocery, payments

    async with session_scope() as session:
        intent = (await session.execute(select(PaymentIntent).where(PaymentIntent.id == uuid.UUID(intent_id))
                                        .with_for_update())).scalar_one()
        if status not in payments.TERMINAL:
            status = "EXPIRED"
        payments.apply_status(intent, status, {"finished_by": "workflow"})
        rupees = grocery.rupees(intent.amount_minor)
        if intent.purpose == "offer_booking":
            return await _finish_offer_payment(session, intent, rupees)
        if intent.purpose != "grocery_order":
            return {"status": intent.status}
        order_id = uuid.UUID(intent.purpose_ref)
        if intent.status == "PAID":
            ref = ",".join(intent.gateway_status.get("payment_ids") or []) or intent.gateway_ref
            order = await grocery.pay_and_place(session, order_id, intent.user_id, payment_ref=f"razorpay:{ref}"[:40])
            await _notify(session, intent.user_id,
                          f"✅ Payment of {rupees} confirmed by Razorpay ({intent.gateway_ref}).\n"
                          f"🧪 TEST order placed · ID {order.order_code} · {grocery.STORES[order.store].name} "
                          f"(sandbox store: nothing will be delivered).", f"payment-done:{intent.id}")
        else:
            await grocery.cancel_order(session, order_id, intent.user_id)
            await _notify(session, intent.user_id,
                          f"❌ Payment of {rupees} was not completed ({intent.status.lower()}). Nothing was ordered. "
                          "Say 'order again' to get a fresh cart.", f"payment-done:{intent.id}")
        return {"status": intent.status}


async def _finish_offer_payment(session: AsyncSession, intent: Any, rupees: str) -> dict[str, Any]:
    from apna.agent.assist import confirm_booking
    from apna.db.models import SandboxBooking

    b = (await session.execute(select(SandboxBooking).where(SandboxBooking.id == uuid.UUID(intent.purpose_ref))
                               .with_for_update())).scalar_one()
    if intent.status == "PAID":
        ref = ",".join(intent.gateway_status.get("payment_ids") or []) or intent.gateway_ref
        if b.status == "CANCELLED":
            b.refund_minor = b.amount_minor
            await _notify(session, intent.user_id, f"⚠️ Razorpay confirmed {rupees} ({intent.gateway_ref}) after this "
                          "booking was cancelled. It is not booked; the refund is recorded (TEST mode: issue it from "
                          "the Razorpay dashboard).", f"payment-done:{intent.id}")
            return {"status": intent.status}
        b = await confirm_booking(session, b.id, intent.user_id, f"razorpay:{ref}")
        await _notify(session, intent.user_id, f"✅ Payment of {rupees} confirmed by Razorpay ({intent.gateway_ref}).\n"
                      f"🧪 TEST booking confirmed · {b.booking_code} · {b.offer['title']} via {b.offer['source_name']} "
                      "(sandbox: no real ticket or order).", f"payment-done:{intent.id}")
    else:
        if b.status in ("HELD", "PAYMENT_PENDING"):
            b.status = "EXPIRED"
        await _notify(session, intent.user_id, f"❌ Payment of {rupees} was not completed ({intent.status.lower()}). "
                      f"Nothing was booked: {b.offer['title']}.", f"payment-done:{intent.id}")
    return {"status": intent.status}


DEFAULT_ACTIVITIES = [reconcile_payment, finish_payment, check_price_watch, reminder_followup, plan_task, set_task_status, prepare_step, execute_step, settle_step, settle_step_many,
                      expire_waiting, cancel_task, publish_reply, finalize_task, deliver_reminder, mark_reminder]
BROWSER_ACTIVITIES = [execute_browser_step]
