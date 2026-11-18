"""Channel adapter contract: normalised inbound events + provider-reported outbound outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class InboundEvent:
    channel: str  # whatsapp | telegram
    external_id: str  # wa_id / telegram chat id (provider-verified sender)
    provider_message_id: str
    kind: str  # text | button | status | unsupported
    text: str = ""
    button_id: str | None = None
    display_name: str | None = None
    timestamp: datetime | None = None
    status: str | None = None  # for kind == status: sent | delivered | read | failed
    raw_type: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SendOutcome:
    status: str  # SENT | FAILED | SKIPPED_NO_CREDENTIALS | BLOCKED_POLICY
    provider_message_id: str | None = None
    error: str | None = None
    kind: str = "session"


class ChannelAdapter(Protocol):
    name: str

    async def send(self, external_id: str, text: str, actions: list[dict[str, Any]], *,
                   last_inbound_at: datetime | None, proactive: bool) -> SendOutcome: ...


def approval_buttons(actions: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """[(button_id, title)] for the first approval action. Titles <= 20 chars (WhatsApp limit)."""
    for a in actions:
        if a.get("type") == "approval" and a.get("approval_id"):
            return [(f"approve:{a['approval_id']}", "✅ Approve"), (f"reject:{a['approval_id']}", "✖ Reject")]
        if a.get("type") == "buttons" and a.get("buttons"):  # e.g. reminder Done / Snooze
            return [(str(b[0]), str(b[1])[:20]) for b in a["buttons"][:3]]
    return []
