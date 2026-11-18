"""Structured logging with secret redaction."""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog

_SECRET_KEYS = re.compile(r"(password|secret|token|api[_-]?key|otp|pin|cvv|authorization)", re.I)


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if _SECRET_KEYS.search(key):
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


log = structlog.get_logger()
