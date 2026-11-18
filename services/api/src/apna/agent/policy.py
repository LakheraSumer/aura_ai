"""Deterministic permission policy engine (spec §21).

This module - not the model - decides whether an action may run. `assert_authorized` is called
inside the executing activity immediately before any side effect.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.agent.tools import ToolSpec
from apna.core.errors import ApprovalRequired, PolicyViolation
from apna.db.models import Approval, UserProfile
from apna.domain.enums import ApprovalStatus, RiskLevel

POLICY_VERSION = "2026-11-12.1"

# Recipient domains the agent may never email autonomously, even with approval, in non-prod.
_BLOCKED_RECIPIENT_SUFFIXES = (".invalid",)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    risk: RiskLevel
    reason: str


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def action_hash(tool: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json({"tool": tool, "args": args}).encode()).hexdigest()


def evaluate(spec: ToolSpec, args: dict[str, Any], profile: UserProfile | None) -> PolicyDecision:
    risk = spec.risk
    if risk == RiskLevel.HIGH_IMPACT:
        return PolicyDecision(False, True, risk, "Level-4 actions are never executed autonomously.")
    if risk == RiskLevel.READ_ONLY:
        return PolicyDecision(True, False, risk, "read-only")
    if risk == RiskLevel.INTERNAL_REVERSIBLE:
        standing = bool(profile and profile.allow_internal_actions)
        return PolicyDecision(True, not standing, risk,
                              "standing permission" if standing else "no standing permission for internal actions")
    if risk == RiskLevel.EXTERNAL_COMMUNICATION:
        recipient = args.get("to_address")
        if spec.integration == "email":
            if not recipient:
                return PolicyDecision(False, True, risk, "recipient is not resolved to a verified address")
            if str(recipient).lower().endswith(_BLOCKED_RECIPIENT_SUFFIXES):
                return PolicyDecision(False, True, risk, "recipient domain is blocked")
        return PolicyDecision(True, True, risk, "external communication needs task-bounded approval")
    # Level 3: transaction-specific approval with amount + merchant + currency.
    for required in ("amount_minor", "currency", "merchant"):
        if required not in args:
            return PolicyDecision(False, True, risk, f"financial action missing {required}")
    return PolicyDecision(True, True, risk, "financial action needs transaction-specific approval")


async def assert_authorized(
    session: AsyncSession, *, step_id: uuid.UUID, spec: ToolSpec, args: dict[str, Any],
    profile: UserProfile | None,
) -> Approval | None:
    """Raise unless this exact action may run now. Consumes the approval (single use)."""
    decision = evaluate(spec, args, profile)
    if not decision.allowed:
        raise PolicyViolation(decision.reason)
    if not decision.requires_approval:
        return None
    approval = (
        await session.execute(select(Approval).where(Approval.step_id == step_id).with_for_update())
    ).scalar_one_or_none()
    if approval is None:
        raise ApprovalRequired("No approval exists for this action.")
    now = datetime.now(UTC)
    if approval.status != ApprovalStatus.APPROVED:
        raise ApprovalRequired(f"Approval is {approval.status}.")
    if approval.expires_at <= now:
        approval.status = ApprovalStatus.EXPIRED
        raise ApprovalRequired("Approval expired.")
    if approval.action_hash != action_hash(spec.name, args):
        raise ApprovalRequired("The action changed after it was approved; re-approval needed.")
    approval.status = ApprovalStatus.CONSUMED
    return approval
