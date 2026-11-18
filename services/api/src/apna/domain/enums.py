"""Domain enums shared by API, agent runtime and workflows."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    UNDERSTANDING = "UNDERSTANDING"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_EXTERNAL_RESPONSE = "WAITING_FOR_EXTERNAL_RESPONSE"
    WAITING_FOR_SCHEDULE = "WAITING_FOR_SCHEDULE"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_TASK

    @property
    def is_waiting(self) -> bool:
        return self in {
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.WAITING_FOR_EXTERNAL_RESPONSE,
            TaskStatus.WAITING_FOR_SCHEDULE,
        }


_TERMINAL_TASK = {
    TaskStatus.COMPLETED,
    TaskStatus.PARTIALLY_COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.EXPIRED,
}

# Allowed task transitions. Enforced in apna.workflows.projection.set_task_status.
TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.UNDERSTANDING, TaskStatus.PLANNING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.UNDERSTANDING: {TaskStatus.PLANNING, TaskStatus.WAITING_FOR_USER, TaskStatus.FAILED,
                               TaskStatus.CANCELLED},
    TaskStatus.PLANNING: {TaskStatus.READY, TaskStatus.WAITING_FOR_USER, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_FOR_USER, TaskStatus.WAITING_FOR_EXTERNAL_RESPONSE, TaskStatus.WAITING_FOR_SCHEDULE,
        TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.RUNNING,
    },
    TaskStatus.WAITING_FOR_USER: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.EXPIRED,
                                  TaskStatus.VERIFYING, TaskStatus.PLANNING},
    TaskStatus.WAITING_FOR_EXTERNAL_RESPONSE: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.EXPIRED},
    TaskStatus.WAITING_FOR_SCHEDULE: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.VERIFYING},
    TaskStatus.VERIFYING: {TaskStatus.COMPLETED, TaskStatus.PARTIALLY_COMPLETED, TaskStatus.FAILED, TaskStatus.EXPIRED,
                           TaskStatus.CANCELLED,
                           TaskStatus.WAITING_FOR_USER, TaskStatus.WAITING_FOR_SCHEDULE},
}


class StepStatus(StrEnum):
    """Verification-aware step states (spec §6.3)."""

    PLANNED = "PLANNED"
    STARTED = "STARTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_USER_ACTION = "AWAITING_USER_ACTION"
    COMPLETED_LOCALLY = "COMPLETED_LOCALLY"
    SUBMITTED_EXTERNALLY = "SUBMITTED_EXTERNALLY"
    CONFIRMED_BY_PROVIDER = "CONFIRMED_BY_PROVIDER"
    SCHEDULED = "SCHEDULED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    @property
    def is_success(self) -> bool:
        return self in {StepStatus.COMPLETED_LOCALLY, StepStatus.CONFIRMED_BY_PROVIDER, StepStatus.SCHEDULED}

    @property
    def is_terminal(self) -> bool:
        return self.is_success or self in {
            StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.REJECTED, StepStatus.CANCELLED,
            StepStatus.SUBMITTED_EXTERNALLY,
        }


class RiskLevel(IntEnum):
    READ_ONLY = 0
    INTERNAL_REVERSIBLE = 1
    EXTERNAL_COMMUNICATION = 2
    FINANCIAL_CONSEQUENTIAL = 3
    HIGH_IMPACT = 4


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    CONSUMED = "CONSUMED"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Channel(StrEnum):
    WEB = "web"
    MOBILE = "mobile"
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"
    API = "api"


class FeatureStatus(StrEnum):
    """Spec §36 status labels."""

    VERIFIED_WORKING = "VERIFIED_WORKING"
    WORKING_IN_MOCK = "WORKING_IN_MOCK"
    WORKING_IN_SANDBOX = "WORKING_IN_SANDBOX"
    IMPLEMENTED_UNVERIFIED = "IMPLEMENTED_UNVERIFIED"
    BLOCKED_EXTERNAL_ACCESS = "BLOCKED_EXTERNAL_ACCESS"
    FAILED = "FAILED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
