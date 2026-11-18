"""Normalised provider data shared by every commerce, travel and services adapter.

Every adapter maps its own response into these types and keeps anything provider-specific in `details`.
Amounts are integers in minor units (paise), so totals never suffer float rounding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class CapabilityStatus(StrEnum):
    LIVE = "live"
    SANDBOX = "sandbox"
    CREDENTIALS_REQUIRED = "credentials-required"
    PARTNER_ACCESS_REQUIRED = "partner-access-required"
    HANDOFF_ONLY = "handoff-only"
    UNSUPPORTED = "unsupported"

    @property
    def usable(self) -> bool:
        return self in (CapabilityStatus.LIVE, CapabilityStatus.SANDBOX)


class Operation(StrEnum):
    SEARCH = "search"
    GET_DETAILS = "get_details"
    CHECK_SERVICEABILITY = "check_serviceability"
    GET_QUOTE = "get_quote"
    CREATE_CART = "create_cart"
    UPDATE_CART = "update_cart"
    GET_CHECKOUT_SUMMARY = "get_checkout_summary"
    INITIATE_CHECKOUT = "initiate_checkout"
    CREATE_BOOKING = "create_booking"
    GET_STATUS = "get_status"
    CANCEL = "cancel"
    GET_REFUND_STATUS = "get_refund_status"
    HANDOFF = "handoff"  # open the provider's own app/site for the user


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str = "INR"


@dataclass
class PriceBreakdown:
    items_minor: int
    fees_minor: dict[str, int] = field(default_factory=dict)  # e.g. {"delivery": 2500, "handling": 400}
    taxes_minor: int = 0
    discounts_minor: int = 0
    currency: str = "INR"

    @property
    def total_minor(self) -> int:
        return self.items_minor + sum(self.fees_minor.values()) + self.taxes_minor - self.discounts_minor


@dataclass
class QuoteLine:
    provider_product_id: str
    name: str
    qty: int
    pack_size: str  # "500 ml", "6 pcs"
    unit_price_minor: int
    variant: str | None = None
    base_quantity: float | None = None  # in the group's base unit (ml / g / pcs) per pack
    base_unit: str | None = None

    @property
    def total_minor(self) -> int:
        return self.unit_price_minor * self.qty


@dataclass
class Quote:
    provider: str
    lines: list[QuoteLine]
    breakdown: PriceBreakdown
    available: bool
    delivery_estimate_min: int | None
    expires_at: datetime | None
    cancellation_terms: str
    evidence: str  # where the numbers came from ("sandbox", provider API endpoint, ...)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderReference:
    provider: str
    reference: str  # provider's own order / booking id
    status: str
    verified_at: datetime
    evidence: str  # e.g. "status API GET /orders/{id}", "authenticated webhook", "sandbox read-back"
