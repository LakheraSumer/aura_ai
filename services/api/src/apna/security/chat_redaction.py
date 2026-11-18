"""Mask payment and login secrets that users type into chat, before storage or the planner sees them.

Apna never asks for these in chat; if a user sends one anyway, it is replaced with a placeholder and the
user is told not to share it. Detection is conservative: card numbers must pass the Luhn check, and
PIN/OTP/CVV/password values must follow their keyword, so ordinary numbers ("2", "12 eggs", "10:45")
are untouched.
"""

from __future__ import annotations

import re

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_KEYWORDED = [
    ("cvv", re.compile(r"\b(cvv|cvc)\b\s*(?:is|hai|:|-|=)?\s*\d{3,4}\b", re.I)),
    ("pin", re.compile(r"\b(upi\s*pin|atm\s*pin|card\s*pin|mpin|pin)\b\s*(?:is|hai|:|-|=)?\s*\d{4,6}\b", re.I)),
    ("otp", re.compile(r"\b(otp|one[\s-]?time\s*(?:password|code)|verification\s*code)\b\s*(?:is|hai|:|-|=)?\s*\d{4,8}\b",
                       re.I)),
    ("password", re.compile(r"\b(password|passcode|pwd)\b\s*(?:is|hai|:|-|=)\s*\S+", re.I)),
]


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """(text with secrets masked, kinds found)."""
    found: list[str] = []

    def card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found.append("card")
            return "[card number removed]"
        return m.group(0)

    out = _CARD.sub(card, text or "")
    for kind, pat in _KEYWORDED:
        if pat.search(out):
            found.append(kind)
            out = pat.sub(f"[{kind} removed]", out)
    return out, sorted(set(found))
