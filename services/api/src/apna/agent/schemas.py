"""Typed task-plan schema (spec §6.2).

Planners (Claude or rule-based) produce a `DraftPlan` of *intent steps* that may still contain
raw phrases ("kal subah 8 baje"). `apna.agent.grounding` turns it into a `TaskPlan` whose
args are concrete and validated, and whose risk / approval fields are derived from the tool
registry - never taken from the model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DraftStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^s\d{1,2}$")
    tool: str
    description: str = Field(max_length=300)
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class DraftPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(max_length=500)
    constraints: list[str] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    steps: list[DraftStep] = Field(max_length=8)
    reply_hint: str | None = None


class PlanStep(BaseModel):
    key: str
    tool: str
    description: str
    args: dict[str, Any]
    depends_on: list[str]
    risk_level: int
    requires_approval: bool
    integration: str


class TaskPlan(BaseModel):
    goal: str
    language: str
    planner: str
    constraints: list[str]
    entities: dict[str, Any]
    steps: list[PlanStep]
    required_integrations: list[str]
    permission_level: int
    approval_checkpoints: list[str]
    estimated_cost_inr: float
    timeout_seconds: int
    verification_criteria: list[str]
    recovery_policy: dict[str, Any]
