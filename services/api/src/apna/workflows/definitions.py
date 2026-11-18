"""Temporal workflow definitions. Deterministic code only - all I/O happens in activities.

TaskWorkflow owns task execution order, approval waits and clarification waits (ADR-002).
ReminderWorkflow is a durable timer that survives worker and server restarts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

DEFAULT_QUEUE = "apna-tasks"
BROWSER_QUEUE = "apna-browser"

_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                     maximum_interval=timedelta(seconds=30), maximum_attempts=5)
_SHORT = timedelta(seconds=60)


@workflow.defn(name="TaskWorkflow")
class TaskWorkflow:
    def __init__(self) -> None:
        self._decisions: dict[str, str] = {}  # step_key -> APPROVED | REJECTED
        self._cancelled = False
        self._clarified = False

    @workflow.signal
    def approval_decided(self, step_key: str, decision: str) -> None:
        self._decisions[step_key] = decision

    @workflow.signal
    def cancel(self) -> None:
        self._cancelled = True

    @workflow.signal
    def clarified(self) -> None:
        self._clarified = True

    @workflow.query
    def pending_decisions(self) -> dict[str, str]:
        return dict(self._decisions)

    async def _act(self, name: str, *args: object, queue: str = DEFAULT_QUEUE,
                   limit: timedelta = _SHORT) -> dict:
        return await workflow.execute_activity(name, args=list(args), task_queue=queue,
                                               start_to_close_timeout=limit, retry_policy=_RETRY)

    @workflow.run
    async def run(self, task_id: str, approval_ttl_s: int, clarification_ttl_s: int) -> dict:
        plan = await self._act("plan_task", task_id, limit=timedelta(seconds=180))
        if plan.get("failed"):
            return await self._act("finalize_task", task_id)
        steps: dict[str, dict] = {s["key"]: s for s in plan["steps"]}
        state: dict[str, str] = {k: "PLANNED" for k in steps}
        waiting_approval: set[str] = set()
        waiting_user: set[str] = set()
        published_rounds = 0
        await self._act("set_task_status", task_id, "RUNNING")

        while True:
            if self._cancelled:
                await self._act("cancel_task", task_id)
                return await self._act("finalize_task", task_id)

            # 1. Run every step whose dependencies are settled, in parallel.
            ready = [k for k, s in steps.items() if state[k] == "PLANNED"
                     and all(_settled(state[d]) for d in s["depends_on"])]
            if ready:
                outcomes = await asyncio.gather(*(self._run_step(task_id, steps[k], state) for k in ready))
                for k, st in zip(ready, outcomes, strict=True):
                    state[k] = st
                    if st == "AWAITING_APPROVAL":
                        waiting_approval.add(k)
                    elif st == "AWAITING_USER_ACTION":
                        waiting_user.add(k)
                continue

            if not waiting_approval and not waiting_user:
                break

            # 2. Something needs the human: publish the current state, then wait durably.
            published_rounds += 1
            await self._act("set_task_status", task_id, "WAITING_FOR_USER")
            await self._act("publish_reply", task_id, published_rounds)
            ttl = approval_ttl_s if waiting_approval else clarification_ttl_s
            try:
                await workflow.wait_condition(
                    lambda: self._cancelled or self._clarified
                    or any(k in self._decisions for k in waiting_approval),
                    timeout=timedelta(seconds=ttl),
                )
            except TimeoutError:
                await self._act("expire_waiting", task_id, sorted(waiting_approval | waiting_user))
                for k in waiting_approval | waiting_user:
                    state[k] = "CANCELLED"
                waiting_approval.clear()
                waiting_user.clear()
                continue
            if self._cancelled:
                continue
            await self._act("set_task_status", task_id, "RUNNING")
            for k in sorted(waiting_approval):
                decision = self._decisions.get(k)
                if decision == "APPROVED":
                    waiting_approval.discard(k)
                    state[k] = await self._execute(task_id, steps[k])
                    if state[k] == "AWAITING_USER_ACTION":
                        waiting_user.add(k)
                elif decision == "REJECTED":
                    waiting_approval.discard(k)
                    await self._act("settle_step", task_id, k, "REJECTED", "Rejected by user")
                    state[k] = "REJECTED"
            if self._clarified and waiting_user:
                await self._act("settle_step_many", task_id, sorted(waiting_user), "SKIPPED",
                                "Superseded by the user's follow-up message")
                for k in waiting_user:
                    state[k] = "SKIPPED"
                waiting_user.clear()
                self._clarified = False

        return await self._act("finalize_task", task_id)

    async def _run_step(self, task_id: str, step: dict, state: dict[str, str]) -> str:
        deps_ok = all(_success(state[d]) for d in step["depends_on"])
        if not deps_ok:
            await self._act("settle_step", task_id, step["key"], "SKIPPED", "A required earlier step did not succeed")
            return "SKIPPED"
        prep = await self._act("prepare_step", task_id, step["key"])
        action = prep["action"]
        if action == "await_approval":
            return "AWAITING_APPROVAL"
        if action == "settled":
            return prep["status"]
        return await self._execute(task_id, step)

    async def _execute(self, task_id: str, step: dict) -> str:
        queue = BROWSER_QUEUE if step.get("task_queue") == "browser" else DEFAULT_QUEUE
        name = "execute_browser_step" if queue == BROWSER_QUEUE else "execute_step"
        out = await self._act(name, task_id, step["key"], queue=queue, limit=timedelta(seconds=180))
        return out["status"]


def _settled(status: str) -> bool:
    return status not in {"PLANNED", "STARTED"}


def _success(status: str) -> bool:
    return status in {"COMPLETED_LOCALLY", "CONFIRMED_BY_PROVIDER", "SCHEDULED"}


@workflow.defn(name="ReminderWorkflow")
class ReminderWorkflow:
    def __init__(self) -> None:
        self._cancelled = False
        self._done = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancelled = True

    @workflow.signal
    def done(self) -> None:
        self._done = True

    @workflow.run
    async def run(self, reminder_id: str, fire_at_utc_iso: str, followup_seconds: int = 0) -> str:
        fire_at = datetime.fromisoformat(fire_at_utc_iso)
        delay = (fire_at - workflow.now()).total_seconds()
        if delay > 0:
            try:
                await workflow.wait_condition(lambda: self._cancelled, timeout=timedelta(seconds=delay))
            except TimeoutError:
                pass  # timer fired normally
        if self._cancelled:
            await workflow.execute_activity("mark_reminder", args=[reminder_id, "CANCELLED"],
                                            start_to_close_timeout=_SHORT, retry_policy=_RETRY)
            return "CANCELLED"
        out = await workflow.execute_activity("deliver_reminder", args=[reminder_id], start_to_close_timeout=_SHORT,
                                              retry_policy=_RETRY)
        if followup_seconds > 0:  # older runs started without a follow-up keep their original behaviour
            try:
                await workflow.wait_condition(lambda: self._done or self._cancelled,
                                              timeout=timedelta(seconds=followup_seconds))
            except TimeoutError:
                await workflow.execute_activity("reminder_followup", args=[reminder_id, int(out.get("snoozes", 0))],
                                                start_to_close_timeout=_SHORT, retry_policy=_RETRY)
        return "DELIVERED"


@workflow.defn(name="PriceWatchWorkflow")
class PriceWatchWorkflow:
    """Checks a price every `poll_seconds` until it drops (message sent), the watch ends, or it is stopped."""

    def __init__(self) -> None:
        self._stop = False

    @workflow.signal
    def stop(self) -> None:
        self._stop = True

    @workflow.run
    async def run(self, watch_id: str, poll_seconds: int) -> str:
        for _ in range(500):  # hard cap; the watch's own end date normally stops it first
            try:
                await workflow.wait_condition(lambda: self._stop, timeout=timedelta(seconds=poll_seconds))
            except TimeoutError:
                pass
            if self._stop:
                return "STOPPED"
            out = await workflow.execute_activity("check_price_watch", args=[watch_id], start_to_close_timeout=_SHORT,
                                                  retry_policy=_RETRY)
            if out.get("done"):
                return "DONE"
        return "CAPPED"


@workflow.defn(name="PaymentWorkflow")
class PaymentWorkflow:
    """Payment pending → paid / failed / expired, exactly once.

    The gateway webhook (verified + deduplicated) signals `payment_event` for a fast path; the workflow
    also reconciles by polling the gateway's status API, so a lost webhook or a worker crash can't leave
    a paid order unplaced or place an unpaid one. Completion runs once (idempotent activity).
    """

    def __init__(self) -> None:
        self._event = False

    @workflow.signal
    def payment_event(self) -> None:
        self._event = True

    @workflow.run
    async def run(self, intent_id: str, expires_at_iso: str, poll_seconds: int = 60) -> str:
        deadline = datetime.fromisoformat(expires_at_iso) + timedelta(minutes=5)  # grace for late webhooks
        status = "PENDING"
        while True:
            try:
                await workflow.wait_condition(lambda: self._event, timeout=timedelta(seconds=poll_seconds))
            except TimeoutError:
                pass
            self._event = False
            status = await workflow.execute_activity("reconcile_payment", args=[intent_id],
                                                     start_to_close_timeout=_SHORT, retry_policy=_RETRY)
            if status in ("PAID", "FAILED", "EXPIRED", "CANCELLED"):
                break
            if workflow.now() >= deadline:
                status = "EXPIRED"
                break
        await workflow.execute_activity("finish_payment", args=[intent_id, status], start_to_close_timeout=_SHORT,
                                        retry_policy=_RETRY)
        return status
