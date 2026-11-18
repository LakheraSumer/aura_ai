"""Grocery requests → equivalent-quantity quotes per store (sandbox provider; pure functions).

- "2 litre milk" is a quantity (2000 ml), not "2 packs": each store gets the cheapest mix of its in-stock
  pack sizes of the same product line that reaches it (2×1 L vs 4×500 ml), so totals are comparable.
- Product lines (Amul Taaza toned vs Amul Gold full cream) are different products: if the user didn't
  say which, the caller asks. Apna never silently swaps one line for another.
- Each store quote includes delivery and handling fees, serviceability for the city and minimum order.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field

from apna.providers.grocery import _GROUPS, STORES, Product, Store, find_group, price_at, rupees
from apna.providers.types import PriceBreakdown, QuoteLine

# Sandbox serviceability and minimum orders (simulated; real values differ by provider and pin code).
_METROS = {"pune", "mumbai", "navi mumbai", "thane", "delhi", "new delhi", "gurgaon", "gurugram", "noida",
           "bengaluru", "bangalore", "hyderabad", "chennai", "kolkata", "ahmedabad"}
_TIER2 = {"jaipur", "lucknow", "chandigarh", "indore", "surat", "vadodara", "nagpur", "nashik", "kochi",
          "coimbatore", "bhopal", "aurangabad", "kolhapur"}
SERVICEABLE: dict[str, set[str]] = {
    "blinkit": _METROS | _TIER2, "instamart": _METROS | _TIER2, "zepto": _METROS | {"jaipur", "lucknow", "nagpur"},
    "flipkart_minutes": _METROS, "bbnow": _METROS | _TIER2,
}
MIN_ORDER_MINOR: dict[str, int] = {"blinkit": 0, "instamart": 0, "zepto": 9900, "flipkart_minutes": 0, "bbnow": 9900}


def parse_amount(size: str | None) -> tuple[float, str] | None:
    """'2 litre' → (2000, 'ml'); '500ml' → (500, 'ml'); '1kg' → (1000, 'g'); '12' / '6 pcs' → (n, 'pcs')."""
    s = (size or "").strip().lower().replace(",", ".")
    if not s:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(ml|millilit\w*|l|ltr|lit\w*|kg|kilo\w*|g|gm|gram\w*|pcs|pc|piece\w*|nos)?$", s)
    if not m:
        return None
    n, unit = float(m.group(1)), (m.group(2) or "pcs")
    if unit.startswith(("l", "lit")) and unit not in ("ml",):
        return n * 1000, "ml"
    if unit.startswith("ml") or unit.startswith("millilit"):
        return n, "ml"
    if unit.startswith("kg") or unit.startswith("kilo"):
        return n * 1000, "g"
    if unit in ("g", "gm") or unit.startswith("gram"):
        return n, "g"
    return n, "pcs"


def product_amount(p: Product) -> tuple[float, str]:
    if re.search(r"\d\s*x\s*\d", p.size):  # multipacks like "4 x 70 g" count as one pack
        return 1, "pack"
    return parse_amount(p.size.replace(" ", "")) or (1, "pack")


def lines_of(group: str) -> list[str]:
    seen: list[str] = []
    for p in _GROUPS[group][2]:
        if p.name not in seen:
            seen.append(p.name)
    return seen


@dataclass
class Request:
    raw: str
    group: str | None
    line: str | None  # chosen product line; None = not chosen yet (ambiguous) or unknown item
    amount: float | None  # total wanted in base units, when the user gave a quantity/size
    unit: str | None
    packs: int  # used when no amount was given: number of default-size packs

    def label(self) -> str:
        if self.amount is not None:
            shown = (f"{self.amount / 1000:g} {'L' if self.unit == 'ml' else 'kg'}"
                     if self.unit in ("ml", "g") and self.amount >= 1000 else f"{self.amount:g} {self.unit}")
            return f"{shown} {self.raw}"
        return f"{self.packs} × {self.raw}"

    def to_args(self) -> dict:
        size = None
        if self.amount is not None:
            size = f"{self.amount:g} {self.unit}" if self.unit != "pcs" else f"{int(self.amount)}"
        return {"name": self.raw, "qty": 1 if self.amount is not None else self.packs, "size": size,
                "brand": self.line}


def make_request(name: str, qty: int = 1, size: str | None = None, brand: str | None = None) -> Request:
    group = find_group(name)
    if group is None:
        return Request(name, None, None, None, None, qty)
    _aliases, counted, products = _GROUPS[group]
    lines = lines_of(group)
    line = None
    if brand:
        b = brand.lower()
        line = next((ln for ln in lines if ln.lower() == b), None) or next((ln for ln in lines if b in ln.lower()), None)
    if line is None and len(lines) == 1:
        line = lines[0]
    amount = parse_amount(size)
    if amount is None and counted and qty > 1:
        amount = (float(qty), "pcs")  # "12 eggs" = 12 pieces
        qty = 1
    if amount is not None:
        return Request(name, group, line, amount[0] * max(1, qty), amount[1], 1)
    return Request(name, group, line, None, None, max(1, qty))


def ambiguous(req: Request) -> list[str]:
    """Product lines to choose from when the user didn't say which (else [])."""
    if req.group is None or req.line is not None:
        return []
    return lines_of(req.group)


def plan_packs(store: Store, req: Request, line: str) -> list[tuple[Product, int]] | None:
    """Cheapest in-stock pack mix of one product line meeting the request (fewest packs on ties)."""
    packs = [p for p in _GROUPS[req.group][2] if p.name == line and p.sku not in store.out_of_stock]  # type: ignore[index]
    if not packs:
        return None
    if req.amount is None:
        default = next((p for p in _GROUPS[req.group][2] if p.name == line), None)  # type: ignore[index]
        if default is None or default.sku in store.out_of_stock:
            default = packs[0]
        return [(default, req.packs)]
    sized = [(p, product_amount(p)) for p in packs]
    sized = [(p, a) for p, (a, u) in sized if u == req.unit]
    if not sized:
        return None
    best: tuple[int, float, int, list[tuple[Product, int]]] | None = None
    limits = [min(20, math.ceil(req.amount / a)) for _p, a in sized]
    for counts in itertools.product(*(range(lim + 1) for lim in limits)):
        total = sum(c * a for c, (_p, a) in zip(counts, sized, strict=True))
        if total < req.amount or total > req.amount * 1.5:  # don't overshoot by more than 50%
            continue
        cost = sum(c * price_at(store, p) for c, (p, _a) in zip(counts, sized, strict=True))
        key = (cost, total - req.amount, sum(counts))
        if best is None or key < best[:3]:
            best = (*key, [(p, c) for c, (p, _a) in zip(counts, sized, strict=True) if c])
    return best[3] if best else None


@dataclass
class StoreQuote:
    store: Store
    lines: list[QuoteLine] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # requests this store can't fulfil (line out of stock)
    reasons: list[str] = field(default_factory=list)  # why the store can't take the order at all
    breakdown: PriceBreakdown | None = None

    @property
    def eligible(self) -> bool:
        return not self.reasons and not self.missing and bool(self.lines)

    @property
    def total(self) -> int:
        return self.breakdown.total_minor if self.breakdown else 0


def quote_store(store: Store, requests: list[Request], city: str | None) -> StoreQuote:
    q = StoreQuote(store)
    if city and city.strip().lower() not in SERVICEABLE.get(store.id, set()):
        q.reasons.append(f"doesn't deliver to {city.title()}")
    for r in requests:
        if r.group is None or r.line is None:
            continue
        plan = plan_packs(store, r, r.line)
        if plan is None:
            q.missing.append(r.raw)
            continue
        for p, count in plan:
            amt, unit = product_amount(p)
            q.lines.append(QuoteLine(p.sku, p.name, count, p.size, price_at(store, p), variant=r.line,
                                     base_quantity=amt, base_unit=unit))
    items = sum(line.total_minor for line in q.lines)
    fees = {}
    if q.lines:
        fees["delivery"] = 0 if items >= store.free_delivery_above_minor else store.delivery_fee_minor
        if store.handling_fee_minor:
            fees["handling"] = store.handling_fee_minor
    q.breakdown = PriceBreakdown(items_minor=items, fees_minor=fees)
    if q.lines and items < MIN_ORDER_MINOR.get(store.id, 0):
        q.reasons.append(f"minimum order is {rupees(MIN_ORDER_MINOR[store.id])}")
    return q


def compare(requests: list[Request], city: str | None, store_ids: list[str] | None = None) -> list[StoreQuote]:
    stores = [STORES[s] for s in (store_ids or list(STORES)) if s in STORES]
    quotes = [quote_store(s, requests, city) for s in stores]
    return sorted(quotes, key=lambda q: (not q.eligible, len(q.missing), q.total, q.store.eta_min))


def option_text(q: StoreQuote) -> str:
    if q.reasons:
        return f"{q.store.name} - not available ({'; '.join(q.reasons)})"
    miss = f" (missing: {', '.join(q.missing)})" if q.missing else ""
    return f"{q.store.name} - {rupees(q.total)} total incl. fees, ~{q.store.eta_min} min{miss}"
