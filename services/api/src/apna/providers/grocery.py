"""Sandbox quick-commerce: a TEST stand-in for grocery delivery partners (status: WORKING_IN_MOCK).

Blinkit, Swiggy Instamart, Zepto, Flipkart Minutes and BB Now have no public ordering API; orders need
the user's own account and payment inside their apps, so Apna never automates them. The real routes are
ONDC (as a registered buyer app) or partner agreements. Until then this sandbox gives the chat flow what
such a provider would: product search, per-store prices and stock, fees, delivery time, an order that is
paid for (TEST payment) and confirmed. Nothing is real: every reply says TEST and no order is placed.

Prices are typical Indian retail prices (MRP-like, November 2026 estimate) and store differences are
simulated. Pure functions (matching, pricing, carts, comparison) are separate from the DB functions so
the logic is unit-tested without a database.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

MERCHANT_SUFFIX = "(TEST sandbox)"


@dataclass(frozen=True)
class Product:
    sku: str
    group: str
    name: str
    size: str
    price_minor: int  # base price, paise


@dataclass(frozen=True)
class Store:
    id: str
    name: str
    eta_min: int
    price_factor: float
    free_delivery_above_minor: int
    delivery_fee_minor: int
    handling_fee_minor: int
    out_of_stock: frozenset[str]


STORES: dict[str, Store] = {s.id: s for s in (
    Store("blinkit", "Blinkit", 10, 1.00, 19900, 2500, 400, frozenset({"saffron-05g", "chicken-500g"})),
    Store("instamart", "Swiggy Instamart", 15, 0.98, 19900, 3000, 500, frozenset({"ghee-1l", "cardamom-25g"})),
    Store("zepto", "Zepto", 10, 1.01, 9900, 2000, 300, frozenset({"paneer-200g", "paneer-500g"})),
    Store("flipkart_minutes", "Flipkart Minutes", 15, 0.97, 24900, 2900, 0, frozenset({"coffee-50g", "saffron-05g"})),
    Store("bbnow", "BB Now", 20, 0.99, 29900, 4000, 0, frozenset({"coke-750ml"})),
)}
_STORE_ALIASES = {
    "blinkit": "blinkit", "grofers": "blinkit", "instamart": "instamart", "swiggy": "instamart",
    "swiggy instamart": "instamart", "zepto": "zepto", "flipkart": "flipkart_minutes",
    "flipkart minutes": "flipkart_minutes", "minutes": "flipkart_minutes", "bigbasket": "bbnow",
    "big basket": "bbnow", "bb now": "bbnow", "bbnow": "bbnow",
}

# group -> (aliases incl. Hindi/Marathi words, counted-in-pieces?, products; the first product is the default)
_GROUPS: dict[str, tuple[tuple[str, ...], bool, tuple[Product, ...]]] = {}


def _g(group: str, aliases: tuple[str, ...], items: list[tuple[str, str, str, int]], counted: bool = False) -> None:
    _GROUPS[group] = (aliases, counted, tuple(Product(sku, group, name, size, rupees * 100)
                                              for sku, name, size, rupees in items))


_g("milk", ("milk", "doodh", "dudh", "दूध"), [
    ("milk-amul-taaza-500ml", "Amul Taaza Toned Milk", "500 ml", 28), ("milk-amul-gold-500ml", "Amul Gold Full Cream Milk", "500 ml", 34),
    ("milk-amul-taaza-1l", "Amul Taaza Toned Milk", "1 l", 56), ("milk-gokul-500ml", "Gokul Full Cream Milk", "500 ml", 36)])
_g("bread", ("bread", "pav", "पाव", "ब्रेड"), [
    ("bread-britannia-white-400g", "Britannia White Bread", "400 g", 45), ("bread-harvest-brown-400g", "Harvest Gold Brown Bread", "400 g", 55),
    ("pav-6", "Ladi Pav", "6 pcs", 30)])
_g("eggs", ("egg", "eggs", "anda", "ande", "अंडे", "अंडी"), [
    ("eggs-6", "Farm Fresh White Eggs", "6 pcs", 54), ("eggs-12", "Farm Fresh White Eggs", "12 pcs", 99),
    ("eggs-30", "Farm Fresh White Eggs", "30 pcs", 230)], counted=True)
_g("rice", ("rice", "basmati", "chawal", "chaawal", "tandul", "चावल", "तांदूळ"), [
    ("rice-indiagate-1kg", "India Gate Basmati Rice Classic", "1 kg", 205), ("rice-daawat-1kg", "Daawat Rozana Basmati Rice", "1 kg", 120),
    ("rice-indiagate-5kg", "India Gate Basmati Rice Classic", "5 kg", 960)])
_g("onion", ("onion", "onions", "pyaz", "pyaaz", "kanda", "प्याज", "कांदा"), [("onion-1kg", "Onion", "1 kg", 45), ("onion-500g", "Onion", "500 g", 24)])
_g("tomato", ("tomato", "tomatoes", "tamatar", "टमाटर", "टोमॅटो"), [("tomato-1kg", "Tomato Hybrid", "1 kg", 40), ("tomato-500g", "Tomato Hybrid", "500 g", 22)])
_g("potato", ("potato", "potatoes", "aloo", "alu", "batata", "आलू", "बटाटा"), [("potato-1kg", "Potato", "1 kg", 38)])
_g("ginger", ("ginger", "adrak", "aale", "अदरक", "आले"), [("ginger-100g", "Ginger", "100 g", 18)])
_g("garlic", ("garlic", "lehsun", "lahsun", "lasun", "लहसुन", "लसूण"), [("garlic-100g", "Garlic", "100 g", 25)])
_g("green chilli", ("green chilli", "green chillies", "hari mirch", "mirchi", "हरी मिर्च", "मिरची"), [("chilli-100g", "Green Chilli", "100 g", 12)])
_g("coriander", ("coriander", "dhaniya", "kothimbir", "धनिया", "कोथिंबीर"), [("coriander-100g", "Coriander Leaves", "100 g", 15)])
_g("mint", ("mint", "pudina", "पुदीना"), [("mint-100g", "Mint Leaves", "100 g", 15)])
_g("lemon", ("lemon", "lemons", "nimbu", "limbu", "नींबू", "लिंबू"), [("lemon-4", "Lemon", "4 pcs", 24)], counted=True)
_g("carrot", ("carrot", "carrots", "gajar", "गाजर"), [("carrot-500g", "Carrot (Ooty)", "500 g", 35)])
_g("beans", ("beans", "french beans", "farasbi"), [("beans-250g", "French Beans", "250 g", 30)])
_g("mixed vegetables", ("mixed vegetables", "mixed veg", "mix veg", "mix vegetables", "vegetables"), [("mixveg-500g", "Safal Frozen Mixed Vegetables", "500 g", 99)])
_g("green peas", ("peas", "green peas", "matar", "मटर", "वाटाणा"), [("peas-500g", "Safal Frozen Green Peas", "500 g", 95)])
_g("cumin", ("cumin", "jeera", "jira", "जीरा", "जिरे"), [("cumin-100g", "Everest Jeera (Cumin Seeds)", "100 g", 70)])
_g("garam masala", ("garam masala",), [("garam-masala-100g", "Everest Garam Masala", "100 g", 82)])
_g("biryani masala", ("biryani masala", "biriyani masala"), [("biryani-masala-50g", "Everest Shahi Biryani Masala", "50 g", 65)])
_g("turmeric", ("turmeric", "haldi", "halad", "हल्दी", "हळद"), [("turmeric-100g", "Everest Turmeric Powder", "100 g", 40)])
_g("red chilli powder", ("chilli powder", "red chilli powder", "lal mirch", "lal tikhat"), [("chilli-powder-100g", "Everest Kashmirilal Chilli Powder", "100 g", 75)])
_g("bay leaf", ("bay leaf", "bay leaves", "tej patta", "tejpatta", "तेजपत्ता"), [("bayleaf-25g", "Bay Leaf (Tej Patta)", "25 g", 25)])
_g("cloves", ("clove", "cloves", "laung", "lavang", "लौंग", "लवंग"), [("cloves-25g", "Cloves (Laung)", "25 g", 45)])
_g("cardamom", ("cardamom", "elaichi", "veldoda", "इलायची", "वेलची"), [("cardamom-25g", "Green Cardamom (Elaichi)", "25 g", 95)])
_g("cinnamon", ("cinnamon", "dalchini", "दालचीनी"), [("cinnamon-50g", "Cinnamon (Dalchini)", "50 g", 55)])
_g("saffron", ("saffron", "kesar", "केसर"), [("saffron-05g", "Saffron (Kesar)", "0.5 g", 160)])
_g("salt", ("salt", "namak", "meeth", "नमक", "मीठ"), [("salt-1kg", "Tata Salt", "1 kg", 28)])
_g("sugar", ("sugar", "cheeni", "chini", "sakhar", "चीनी", "साखर"), [("sugar-1kg", "Sugar", "1 kg", 50)])
_g("atta", ("atta", "wheat flour", "aata", "गेहूं का आटा", "कणिक"), [("atta-5kg", "Aashirvaad Whole Wheat Atta", "5 kg", 290), ("atta-1kg", "Aashirvaad Whole Wheat Atta", "1 kg", 68)])
_g("toor dal", ("dal", "daal", "toor dal", "tur dal", "arhar", "तूर डाळ", "दाल"), [("toor-dal-1kg", "Tata Sampann Toor Dal", "1 kg", 175)])
_g("oil", ("oil", "cooking oil", "sunflower oil", "tel", "तेल"), [("oil-sunflower-1l", "Fortune Sunflower Oil", "1 l", 165)])
_g("ghee", ("ghee", "tup", "घी", "तूप"), [("ghee-500ml", "Amul Pure Ghee", "500 ml", 330), ("ghee-1l", "Amul Pure Ghee", "1 l", 650)])
_g("paneer", ("paneer", "पनीर"), [("paneer-200g", "Amul Fresh Paneer", "200 g", 95), ("paneer-500g", "Amul Fresh Paneer", "500 g", 225)])
_g("curd", ("curd", "dahi", "yogurt", "दही"), [("curd-400g", "Amul Masti Dahi", "400 g", 38)])
_g("butter", ("butter", "makhan", "loni", "मक्खन"), [("butter-100g", "Amul Butter", "100 g", 60)])
_g("cheese", ("cheese",), [("cheese-slices-200g", "Amul Cheese Slices", "200 g", 140)])
_g("chicken", ("chicken", "murgi", "मुर्गी", "चिकन"), [("chicken-500g", "Licious Chicken Curry Cut", "500 g", 190)])
_g("banana", ("banana", "bananas", "kela", "kele", "केला", "केळी"), [("banana-6", "Banana Robusta", "6 pcs", 48)], counted=True)
_g("apple", ("apple", "apples", "seb", "सेब", "सफरचंद"), [("apple-4", "Shimla Apple", "4 pcs", 160)], counted=True)
_g("tea", ("tea", "chai", "chai patti", "chaha", "चाय", "चहा"), [("tea-tata-250g", "Tata Tea Premium", "250 g", 145)])
_g("coffee", ("coffee", "कॉफी"), [("coffee-50g", "Nescafe Classic Instant Coffee", "50 g", 180)])
_g("maggi", ("maggi", "noodles", "मैगी"), [("maggi-4", "Maggi 2-Minute Masala Noodles", "4 x 70 g", 56)])
_g("chips", ("chips", "lays", "wafers"), [("chips-lays-50g", "Lay's Classic Salted", "50 g", 20)])
_g("coke", ("coke", "coca cola", "coca-cola", "cold drink", "soft drink"), [("coke-750ml", "Coca-Cola", "750 ml", 40)])
_g("biscuits", ("biscuit", "biscuits", "parle g", "parle-g", "बिस्किट"), [("parleg-250g", "Parle-G Biscuits", "250 g", 25)])
_g("toothpaste", ("toothpaste", "colgate"), [("colgate-150g", "Colgate Strong Teeth Toothpaste", "150 g", 99)])
_g("soap", ("soap", "sabun", "साबुन"), [("soap-lifebuoy-4", "Lifebuoy Soap", "4 x 100 g", 140)])
_g("detergent", ("detergent", "surf", "washing powder"), [("surf-1kg", "Surf Excel Easy Wash", "1 kg", 145)])
_g("water", ("water", "mineral water", "bisleri", "paani", "पानी"), [("bisleri-1l", "Bisleri Water", "1 l", 20)])

_ALIAS_INDEX = {alias: g for g, (aliases, _c, _p) in _GROUPS.items() for alias in aliases}


def store_named(text: str | None) -> Store | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in _STORE_ALIASES:
        return STORES[_STORE_ALIASES[t]]
    for alias, sid in sorted(_STORE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return STORES[sid]
    close = difflib.get_close_matches(t, list(_STORE_ALIASES), n=1, cutoff=0.8)
    return STORES[_STORE_ALIASES[close[0]]] if close else None


def _norm_size(s: str | None) -> str:
    s = (s or "").lower().replace(" ", "")
    for a, b in (("litres", "l"), ("litre", "l"), ("liters", "l"), ("liter", "l"), ("ltr", "l"), ("kgs", "kg"),
                 ("grams", "g"), ("gram", "g"), ("gm", "g"), ("pieces", "pcs"), ("piece", "pcs"), ("pc", "pcs")):
        s = s.replace(a, b)
    s = s.replace("pcss", "pcs")
    return s.removesuffix("pcs") if re.fullmatch(r"\d+pcs", s) else s


def find_group(name: str) -> str | None:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not n:
        return None
    if n in _ALIAS_INDEX:
        return _ALIAS_INDEX[n]
    hits = [(len(a), g) for a, g in _ALIAS_INDEX.items() if re.search(rf"(?<![\wऀ-ॿ]){re.escape(a)}(?![\wऀ-ॿ])", n)]
    if hits:
        return max(hits)[1]  # longest alias wins ("biryani masala" over "masala")
    close = difflib.get_close_matches(n, list(_ALIAS_INDEX), n=1, cutoff=0.82)
    return _ALIAS_INDEX[close[0]] if close else None


def price_at(store: Store, p: Product) -> int:
    return int(round(p.price_minor * store.price_factor / 100.0)) * 100  # whole rupees


def rupees(minor: int) -> str:
    return f"₹{minor // 100:,}" if minor % 100 == 0 else f"₹{minor / 100:,.2f}"


def product_by_sku(sku: str) -> Product | None:
    for _aliases, _counted, products in _GROUPS.values():
        for p in products:
            if p.sku == sku:
                return p
    return None


CANCELLATION_TERMS = ("TEST sandbox: cancellable free until the order is packed; refunds go back to the original "
                      "payment method.")


# ------------------------------------------------------------------ DB: carts and TEST orders


def _line_dict(line) -> dict:  # QuoteLine -> stored line
    return {"sku": line.provider_product_id, "name": line.name, "size": line.pack_size, "qty": line.qty,
            "unit_minor": line.unit_price_minor, "variant": line.variant}


async def save_cart(session: AsyncSession, *, user_id: uuid.UUID, idempotency_key: str, quote, requests: list[dict],
                    city: str, area: str | None, ttl_minutes: int):
    """Persist a priced cart (one store's quote). A new cart replaces the user's older unpaid carts, so an
    old Pay button can't place a stale order."""
    from datetime import timedelta

    from apna.db.models import SandboxGroceryOrder as O

    existing = (await session.execute(select(O).where(O.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing:
        return existing
    await session.execute(update(O).where(O.user_id == user_id, O.status == "CART").values(status="REPLACED"))
    b = quote.breakdown
    order = O(user_id=user_id, idempotency_key=idempotency_key, store=quote.store.id, city=city, area=area,
              items=[_line_dict(x) for x in quote.lines], request=requests,
              item_total_minor=b.items_minor, delivery_fee_minor=b.fees_minor.get("delivery", 0),
              handling_fee_minor=b.fees_minor.get("handling", 0), total_minor=b.total_minor,
              eta_minutes=quote.store.eta_min, status="CART",
              quote_expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes))
    session.add(order)
    await session.flush()
    return order


def reprice(order) -> int:
    """Current total for the exact same lines at the same store (quotes expire; prices can move)."""
    store = STORES[order.store]
    items = 0
    for x in order.items:
        p = product_by_sku(x["sku"])
        if p is None or p.sku in store.out_of_stock:
            return -1  # no longer available
        items += price_at(store, p) * x["qty"]
    delivery = 0 if items >= store.free_delivery_above_minor else store.delivery_fee_minor
    return items + delivery + store.handling_fee_minor


async def latest_cart(session: AsyncSession, user_id: uuid.UUID):
    from apna.db.models import SandboxGroceryOrder as O

    return (await session.execute(select(O).where(O.user_id == user_id, O.status == "CART")
                                  .order_by(O.created_at.desc()))).scalars().first()


class CartReplaced(Exception):
    pass


async def mark_payment_pending(session: AsyncSession, order_id: uuid.UUID, user_id: uuid.UUID):
    from apna.db.models import SandboxGroceryOrder as O

    o = (await session.execute(select(O).where(O.id == order_id).with_for_update())).scalar_one()
    if o.user_id != user_id:
        raise PermissionError("order belongs to another user")
    if o.status not in ("CART", "PAYMENT_PENDING"):
        raise CartReplaced(o.status)
    o.status = "PAYMENT_PENDING"
    await session.flush()
    return o


async def pay_and_place(session: AsyncSession, order_id: uuid.UUID, user_id: uuid.UUID, *,
                        payment_ref: str | None = None):
    """Place the order after payment. Idempotent: a placed order returns the same order. With no gateway
    reference this is the TEST sandbox payment; with one, the caller has verified the gateway's PAID status."""
    from apna.db.models import SandboxGroceryOrder as O

    o = (await session.execute(select(O).where(O.id == order_id).with_for_update())).scalar_one()
    if o.user_id != user_id:
        raise PermissionError("order belongs to another user")
    if o.status == "PLACED":
        return o
    if o.status not in ("CART", "PAYMENT_PENDING"):
        raise CartReplaced(o.status)
    o.status = "PLACED"
    o.payment_ref = payment_ref or "TESTPAY-" + hashlib.sha256(f"gpay:{o.id}".encode()).hexdigest()[:10].upper()
    o.order_code = "TEST-ORD-" + hashlib.sha256(f"gord:{o.id}".encode()).hexdigest()[:8].upper()
    o.placed_at = datetime.now(UTC)
    await session.flush()
    return o


async def cancel_order(session: AsyncSession, order_id: uuid.UUID, user_id: uuid.UUID) -> str:
    from apna.db.models import SandboxGroceryOrder as O

    o = (await session.execute(select(O).where(O.id == order_id).with_for_update())).scalar_one()
    if o.user_id != user_id:
        raise PermissionError("order belongs to another user")
    if o.status in ("CART", "PAYMENT_PENDING"):
        o.status = "CANCELLED"
    await session.flush()
    return o.status


async def get_order(session: AsyncSession, code: str):
    from apna.db.models import SandboxGroceryOrder as O

    return (await session.execute(select(O).where(O.order_code == code))).scalar_one_or_none()
