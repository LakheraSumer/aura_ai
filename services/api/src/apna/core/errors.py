"""Explicit error classes. Each maps to a stable API error code."""

from __future__ import annotations


class ApnaError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(ApnaError):
    code = "not_found"
    http_status = 404


class Unauthorized(ApnaError):
    code = "unauthorized"
    http_status = 401


class Forbidden(ApnaError):
    code = "forbidden"
    http_status = 403


class Conflict(ApnaError):
    code = "conflict"
    http_status = 409


class InvalidInput(ApnaError):
    code = "invalid_input"
    http_status = 422


class ApprovalRequired(ApnaError):
    """Raised when a side-effecting tool is executed without a matching approval."""

    code = "approval_required"
    http_status = 403


class PolicyViolation(ApnaError):
    code = "policy_violation"
    http_status = 403


class ProviderUnavailable(ApnaError):
    """A provider is not configured / not authorised in this environment."""

    code = "provider_unavailable"
    http_status = 503


class UserHandoffRequired(ApnaError):
    """The task hit a password / OTP / CAPTCHA wall; a human must take over."""

    code = "user_handoff_required"
    http_status = 409
