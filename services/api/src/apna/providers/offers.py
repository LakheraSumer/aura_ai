"""Offers engine (sandbox): compare the SAME product or service across sources, cheapest total first.

Status: WORKING_IN_MOCK. The real sellers (Amazon, Flipkart, Swiggy, Zomato, MakeMyTrip, Booking.com,
Agoda, Trip.com, redBus, BookMyShow, Uber, Ola, Rapido, ...) don't give outside apps price or booking
access (docs/integrations/CAPABILITY_MATRIX.md), so prices, availability and bookings here are TEST data.
Every reply says TEST and links the real seller's own page (link formats verified in a browser).

How comparison works:
- Offers for the same item (same product model, same dish from the same restaurant, same flight, same
  hotel and dates, same bus, same event tier, same ride type) are priced at every source that sells it.
- The total includes everything the user would pay: item price × quantity, delivery / convenience /
  platform / booking fees, taxes, minus instant discounts. Ranking uses that total, never the headline.
- Conditions that matter (baggage, refundability, cancellation deadline, return window) are carried with
  each offer and shown before approval.

Pure functions only (deterministic per day), so tests and price-drop watches are reproducible. Prices
drift a few percent from day to day, which is what price-drop alerts react to.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, quote_plus
from zoneinfo import ZoneInfo

from apna.providers.types import PriceBreakdown

IST = ZoneInfo("Asia/Kolkata")
VERTICALS = ("product", "food", "flight", "hotel", "bus", "event", "table", "ride")


def _h(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def _f(*parts: Any, lo: float, hi: float) -> float:
    return lo + (_h(*parts) % 10_000) / 10_000 * (hi - lo)


def _r(x: float) -> int:
    """Whole rupees, in paise."""
    return int(round(x)) * 100


def rupees(minor: int) -> str:
    return f"₹{minor // 100:,}" if minor % 100 == 0 else f"₹{minor / 100:,.2f}"


def _drift(*key: Any, day: date) -> float:
    return 1 + _f(*key, day.isoformat(), lo=-0.06, hi=0.06)  # ±6% day to day


# ------------------------------------------------------------------ sources (real names, verified links)

SOURCES: dict[str, dict[str, Any]] = {
    "amazon": {"name": "Amazon", "link": lambda p: f"https://www.amazon.in/s?k={quote_plus(p.get('q', ''))}"},
    "flipkart": {"name": "Flipkart", "link": lambda p: f"https://www.flipkart.com/search?q={quote_plus(p.get('q', ''))}"},
    "croma": {"name": "Croma", "link": lambda p: "https://www.croma.com/"},
    "reliance_digital": {"name": "Reliance Digital", "link": lambda p: "https://www.reliancedigital.in/"},
    "myntra": {"name": "Myntra", "link": lambda p: "https://www.myntra.com/"},
    "swiggy_food": {"name": "Swiggy", "link": lambda p: "https://www.swiggy.com/"},
    "zomato": {"name": "Zomato", "link": lambda p: f"https://www.zomato.com/{p.get('city', 'pune')}/restaurants"},
    "makemytrip": {"name": "MakeMyTrip", "link": lambda p: "https://www.makemytrip.com/"},
    "tripcom": {"name": "Trip.com", "link": lambda p: "https://www.trip.com/"},
    "booking_com": {"name": "Booking.com", "link": lambda p: (
        "https://www.booking.com/searchresults.html?ss=" + quote_plus(p.get("city", ""))
        + (f"&checkin={p['checkin']}&checkout={p['checkout']}" if p.get("checkin") else "")
        + f"&group_adults={p.get('adults', 2)}&no_rooms={p.get('rooms', 1)}")},
    "agoda": {"name": "Agoda", "link": lambda p: "https://www.agoda.com/"},
    "airline": {"name": "Airline website", "link": lambda p: "https://www.goindigo.in/"},  # per airline below
    "redbus": {"name": "redBus", "link": lambda p: (
        f"https://www.redbus.in/bus-tickets/{_slug(p.get('origin', ''))}-to-{_slug(p.get('destination', ''))}"
        if p.get("origin") else "https://www.redbus.in/")},
    "bookmyshow": {"name": "BookMyShow", "link": lambda p: f"https://in.bookmyshow.com/explore/events-{_slug(p.get('city', 'pune'))}"},
    "district": {"name": "District (by Zomato)", "link": lambda p: "https://www.district.in/"},
    "swiggy_dineout": {"name": "Swiggy Dineout", "link": lambda p: "https://www.swiggy.com/dineout"},
    "uber": {"name": "Uber", "link": lambda p: "https://m.uber.com/ul/?action=setPickup"
             + (f"&pickup[formatted_address]={quote(p['pickup'])}" if p.get("pickup") else "&pickup=my_location")
             + (f"&dropoff[formatted_address]={quote(p['dropoff'])}" if p.get("dropoff") else "")},
    "ola": {"name": "Ola", "link": lambda p: "https://www.olacabs.com/"},
    "rapido": {"name": "Rapido", "link": lambda p: "https://www.rapido.bike/"},
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# ------------------------------------------------------------------ data model


@dataclass
class OfferQuery:
    vertical: str
    query: str = ""
    city: str | None = None
    area: str | None = None
    origin: str | None = None
    destination: str | None = None
    day: date | None = None  # travel / event / reservation day
    checkin: date | None = None
    checkout: date | None = None
    qty: int = 1  # units, tickets, travellers, guests, people
    rooms: int = 1
    time_pref: str | None = None  # "morning", "evening", "after 6 pm", "8 pm"
    class_pref: str | None = None  # "AC sleeper", "economy", "bike", "sedan"
    sources: list[str] = field(default_factory=list)
    budget_max_minor: int | None = None
    sort: str = "cheapest"  # cheapest | fastest | earliest | best_rated


@dataclass
class Offer:
    id: str
    vertical: str
    source: str
    source_name: str
    item_key: str  # identical item across sources (the comparison unit)
    title: str
    subtitle: str
    qty: int
    unit_minor: int
    breakdown: PriceBreakdown
    eta: str = ""
    when: str | None = None  # ISO local datetime for travel / events / tables
    conditions: dict[str, str] = field(default_factory=dict)
    rating: float | None = None
    duration_min: int | None = None
    link: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.breakdown.total_minor

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["breakdown"] = {"items_minor": self.breakdown.items_minor, "fees_minor": self.breakdown.fees_minor,
                          "taxes_minor": self.breakdown.taxes_minor, "discounts_minor": self.breakdown.discounts_minor,
                          "total_minor": self.total}
        return d


def _offer_id(*parts: Any) -> str:
    return "OFR-" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:8].upper()


def _src_link(source: str, params: dict[str, Any]) -> str:
    return SOURCES[source]["link"](params)


# ------------------------------------------------------------------ products

_PRODUCTS: list[tuple[str, str, int, tuple[str, ...]]] = [
    # (name, category, base MRP ₹, extra aliases)
    ("Apple iPhone 16 (128 GB)", "electronics", 79900, ("iphone 16",)),
    ("Apple iPhone 16 Pro (256 GB)", "electronics", 129900, ("iphone 16 pro",)),
    ("Samsung Galaxy S25 (256 GB)", "electronics", 80999, ("galaxy s25", "s25")),
    ("OnePlus 13R (12 GB / 256 GB)", "electronics", 42999, ("oneplus 13r",)),
    ("boAt Airdopes 141", "electronics", 1299, ("airdopes 141", "boat earbuds")),
    ("Sony WH-1000XM5 Headphones", "electronics", 29990, ("sony xm5", "wh-1000xm5")),
    ("Apple AirPods Pro (2nd gen)", "electronics", 24900, ("airpods pro",)),
    ("Kindle Paperwhite (16 GB)", "electronics", 16999, ("kindle",)),
    ("HP Victus 15 Gaming Laptop (i5, RTX 3050)", "electronics", 62990, ("hp victus", "gaming laptop")),
    ("Apple MacBook Air M3 13-inch (8 GB / 256 GB)", "electronics", 114900, ("macbook air", "macbook")),
    ("Samsung 1.5 Ton 5 Star Inverter Split AC", "appliances", 46990, ("split ac", "samsung ac")),
    ("LG 7 kg Front Load Washing Machine", "appliances", 33990, ("washing machine",)),
    ("Philips Air Fryer HD9252", "appliances", 8999, ("air fryer",)),
    ("Prestige Iris 750W Mixer Grinder", "home", 3299, ("mixer grinder", "mixer")),
    ("Bajaj Majesty Electric Kettle 1.5 L", "home", 1149, ("electric kettle", "kettle")),
    ("Fire-Boltt Ninja Call Pro Smartwatch", "electronics", 1999, ("smartwatch", "fire boltt")),
    ("Nike Revolution 7 Running Shoes", "fashion", 3695, ("nike shoes", "running shoes")),
    ("Levi's 511 Slim Fit Jeans", "fashion", 3299, ("levis jeans", "jeans")),
    ("Atomic Habits (Paperback)", "books", 499, ("atomic habits",)),
]
_PRODUCT_SOURCES = {"electronics": ["amazon", "flipkart", "croma", "reliance_digital"],
                    "appliances": ["amazon", "flipkart", "croma", "reliance_digital"],
                    "home": ["amazon", "flipkart"], "books": ["amazon", "flipkart"],
                    "fashion": ["amazon", "flipkart", "myntra"]}
_PRODUCT_TERMS = {"amazon": ("1-2 days", "7-day replacement"), "flipkart": ("2-3 days", "7-day replacement"),
                  "croma": ("2-4 days or store pickup", "7-day return"), "reliance_digital": ("3-5 days", "7-day return"),
                  "myntra": ("3-5 days", "14-day return")}


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def match_products(query: str) -> list[tuple[float, str]]:
    """(score, product name), best first. Exact model tokens matter ('16 pro' vs '16')."""
    q = _tokens(query)
    out = []
    for name, _cat, _base, aliases in _PRODUCTS:
        best = 0.0
        for cand in (name, *aliases):
            t = _tokens(cand)
            if not t:
                continue
            hit = len(q & t) / len(t)
            extra = len(q - t - {"buy", "price", "best", "cheapest", "sasta", "online", "the", "a", "for", "me",
                                 "kharidna", "order", "compare", "chahiye", "dikhao"})
            best = max(best, hit - 0.08 * extra)
        if best >= 0.5:
            out.append((round(best, 3), name))
    return sorted(out, key=lambda x: -x[0])


def product_offers(q: OfferQuery, product: str, day: date) -> list[Offer]:
    name, cat, base, _a = next(p for p in _PRODUCTS if p[0] == product)
    out = []
    for src in _PRODUCT_SOURCES[cat]:
        if q.sources and src not in q.sources:
            continue
        if _h(src, name, day) % 9 == 0:  # some stores are out of stock on some days
            continue
        price = _r(base * _f(src, name, lo=0.86, hi=1.0) * _drift(src, name, day=day))
        items = price * q.qty
        fees = {"delivery": 0 if items >= 49900 else 4000}
        disc = _r(min(items * 0.10 / 100, 1500)) if src == ("flipkart" if _h(name, day) % 2 else "amazon") and base > 10000 else 0
        eta, returns = _PRODUCT_TERMS[src]
        out.append(Offer(_offer_id("product", src, name, day, q.qty), "product", src, SOURCES[src]["name"], name, name,
                         f"Sold by {SOURCES[src]['name']}", q.qty, price,
                         PriceBreakdown(items_minor=items, fees_minor=fees, discounts_minor=disc),
                         eta=f"Delivery {eta}", conditions={"returns": returns,
                                                            "offer": "Instant bank discount applied" if disc else ""},
                         rating=round(_f(name, "rating", lo=4.0, hi=4.6), 1),
                         link=_src_link(src, {"q": name}), details={"category": cat}))
    return out


# ------------------------------------------------------------------ food (same dish, same restaurant, Swiggy vs Zomato)

_RESTAURANTS = [
    ("Royal Biryani House", {"chicken biryani": 289, "veg biryani": 229, "mutton biryani": 389, "raita": 49}),
    ("Pizza Corner", {"margherita pizza": 249, "farmhouse pizza": 399, "garlic bread": 129, "paneer pizza": 349}),
    ("Punjabi Tadka", {"paneer butter masala": 279, "dal makhani": 229, "butter naan": 49, "chicken biryani": 309}),
    ("South Café", {"masala dosa": 129, "idli vada": 99, "filter coffee": 49, "veg biryani": 199}),
    ("Momo Junction", {"chicken momos": 169, "veg momos": 139, "chilli chicken": 249}),
    ("Spice Route Kitchen", {"chicken biryani": 269, "butter chicken": 329, "butter naan": 45, "gulab jamun": 89}),
]
_FOOD_ALIASES = {"biryani": "chicken biryani", "pizza": "margherita pizza", "dosa": "masala dosa", "momos": "veg momos",
                 "paneer": "paneer butter masala", "naan": "butter naan", "chai": "filter coffee"}


def food_offers(q: OfferQuery, day: date) -> list[Offer]:
    want = q.query.lower().strip()
    dish = next((d for _r_, menu in _RESTAURANTS for d in menu if d in want), None) or _FOOD_ALIASES.get(
        next((w for w in _FOOD_ALIASES if w in want), ""), None)
    if not dish:
        return []
    city = (q.city or "Pune").title()
    out = []
    for rest, menu in _RESTAURANTS:
        if dish not in menu:
            continue
        for src in ("swiggy_food", "zomato"):
            if q.sources and src not in q.sources:
                continue
            unit = _r(menu[dish] * _f(src, rest, dish, lo=1.0, hi=1.08) * _drift(src, rest, dish, day=day))
            items = unit * q.qty
            fees = {"delivery": _r(_f(src, rest, city, lo=0, hi=45)), "platform": 1000 if src == "swiggy_food" else 1200,
                    "packaging": 2000}
            taxes = int(items * 0.05)
            promo = _h(src, rest, day) % 3 == 0 and items >= 29900
            disc = min(int(items * 0.2), 8000) if promo else 0
            eta = int(_f(src, rest, city, "eta", lo=25, hi=45))
            out.append(Offer(_offer_id("food", src, rest, dish, day, q.qty), "food", src, SOURCES[src]["name"],
                             f"{rest}|{dish}", f"{dish.title()} × {q.qty} · {rest}", f"via {SOURCES[src]['name']}",
                             q.qty, unit, PriceBreakdown(items_minor=items, fees_minor=fees, taxes_minor=taxes,
                                                         discounts_minor=disc),
                             eta=f"~{eta} min", conditions={"offer": "20% off (max ₹80)" if disc else "",
                                                           "cancellation": "Cancellable until the restaurant accepts"},
                             rating=round(_f(rest, "r", lo=3.9, hi=4.5), 1), duration_min=eta,
                             link=_src_link(src, {"city": _slug(city)}), details={"restaurant": rest, "dish": dish}))
    return out


# ------------------------------------------------------------------ flights

_AIRPORTS = {"pune": ("PNQ", 18.58, 73.92), "mumbai": ("BOM", 19.09, 72.87), "delhi": ("DEL", 28.56, 77.10),
             "new delhi": ("DEL", 28.56, 77.10), "bengaluru": ("BLR", 13.20, 77.71), "bangalore": ("BLR", 13.20, 77.71),
             "hyderabad": ("HYD", 17.24, 78.43), "chennai": ("MAA", 12.99, 80.17), "kolkata": ("CCU", 22.65, 88.45),
             "goa": ("GOI", 15.38, 73.83), "jaipur": ("JAI", 26.82, 75.81), "ahmedabad": ("AMD", 23.07, 72.63),
             "nagpur": ("NAG", 21.09, 79.05), "lucknow": ("LKO", 26.76, 80.89), "kochi": ("COK", 10.15, 76.39)}
_AIRLINES = [("IndiGo", "6E", 15, "cancellation fee ₹3,500"), ("Air India", "AI", 15, "cancellation fee ₹3,000"),
             ("Akasa Air", "QP", 15, "cancellation fee ₹3,250"), ("Air India Express", "IX", 15, "cancellation fee ₹2,999"),
             ("SpiceJet", "SG", 15, "non-refundable")]


def _km(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[1], a[2], b[1], b[2]))
    d = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


def airport(city: str | None) -> tuple | None:
    return _AIRPORTS.get((city or "").strip().lower())


def flight_offers(q: OfferQuery, today: date) -> list[Offer]:
    a, b = airport(q.origin), airport(q.destination)
    if not a or not b or a[0] == b[0]:
        return []
    day = q.day or today + timedelta(days=1)
    km = _km(a, b)
    out = []
    for airline, code, bag, cancel in _AIRLINES:
        for j in range(2):
            if _h(airline, a[0], b[0], day, j) % 3 == 0:
                continue  # not every airline flies every slot
            dep = time(6 + (_h(code, j, a[0], b[0]) % 16), (_h(code, j, day) % 4) * 15)
            dur = int(45 + km / 7.5)
            ahead = max((day - today).days, 0)
            fare = (2200 + km * 4.1) * (1.6 if ahead < 3 else 1.25 if ahead < 10 else 1.0) * _f(airline, j, lo=0.9, hi=1.25)
            fno = f"{code}-{100 + _h(code, a[0], b[0], j) % 900}"
            when = datetime.combine(day, dep, IST)
            for src in ("airline", "makemytrip", "tripcom", "booking_com"):
                if q.sources and src not in q.sources:
                    continue
                unit = _r(fare * _drift(src, fno, day=today) * (1.0 if src == "airline" else _f(src, fno, lo=0.97, hi=1.04)))
                items = unit * q.qty
                taxes = _r(700 + km * 0.25) * q.qty
                fees = {"convenience": {"airline": 0, "makemytrip": 35000, "tripcom": 0, "booking_com": 24900}[src]
                        * q.qty // 1}
                name = airline if src != "airline" else f"{airline} website"
                out.append(Offer(_offer_id("flight", src, fno, day, q.qty), "flight", src,
                                 name if src == "airline" else SOURCES[src]["name"], f"{fno}|{day}",
                                 f"{airline} {fno} · {a[0]} {dep:%H:%M} → {b[0]} "
                                 f"{(when + timedelta(minutes=dur)):%H:%M}", f"{dur // 60}h {dur % 60}m · non-stop",
                                 q.qty, unit, PriceBreakdown(items_minor=items, fees_minor=fees, taxes_minor=taxes),
                                 when=when.isoformat(), duration_min=dur,
                                 conditions={"baggage": f"Cabin 7 kg + check-in {bag} kg", "cancellation": cancel,
                                             "fare": "Saver fare"},
                                 link=_AIRLINE_SITES[airline] if src == "airline" else _FLIGHT_SITES[src],
                                 details={"airline": airline, "flight": fno, "from": a[0], "to": b[0]}))
    return out


# Official sites (the hotel-search deep link of Booking.com doesn't apply to flights).
_AIRLINE_SITES = {"IndiGo": "https://www.goindigo.in/", "Air India": "https://www.airindia.com/",
                  "Akasa Air": "https://www.akasaair.com/", "Air India Express": "https://www.airindiaexpress.com/",
                  "SpiceJet": "https://www.spicejet.com/"}
_FLIGHT_SITES = {"makemytrip": "https://www.makemytrip.com/flights/", "tripcom": "https://www.trip.com/flights/",
                 "booking_com": "https://www.booking.com/flights/"}


# ------------------------------------------------------------------ hotels

_HOTEL_NAMES = ["Sunrise Residency", "The Orchid Stay", "Palm Grove Inn", "City Comfort Suites", "Lakeview Retreat",
                "Heritage Courtyard"]


def hotel_offers(q: OfferQuery, today: date) -> list[Offer]:
    city = (q.city or q.destination or "").strip()
    if not city:
        return []
    ci = q.checkin or today + timedelta(days=1)
    co = q.checkout if q.checkout and q.checkout > ci else ci + timedelta(days=1)
    nights = (co - ci).days
    out = []
    for name in _HOTEL_NAMES:
        hotel = f"{name}, {city.title()}"
        base = _f(hotel, lo=1800, hi=9500)
        stars = 3 + _h(hotel) % 3
        breakfast = _h(hotel, "bf") % 2 == 0
        for src in ("booking_com", "agoda", "makemytrip", "tripcom"):
            if q.sources and src not in q.sources:
                continue
            night = _r(base * _f(src, hotel, lo=0.92, hi=1.08) * _drift(src, hotel, day=ci))
            items = night * nights * q.rooms
            taxes = int(items * (0.12 if night <= 750000 else 0.18))
            free_cancel = _h(src, hotel) % 3 != 0
            out.append(Offer(_offer_id("hotel", src, hotel, ci, co, q.rooms), "hotel", src, SOURCES[src]["name"],
                             f"{hotel}|{ci}|{co}|{q.rooms}", hotel, f"{stars}★ · {nights} night(s) · {q.rooms} room(s)",
                             q.rooms, night, PriceBreakdown(items_minor=items, taxes_minor=taxes),
                             when=datetime.combine(ci, time(14), IST).isoformat(),
                             conditions={"cancellation": f"Free cancellation until {ci - timedelta(days=1):%d %b}"
                                         if free_cancel else "Non-refundable",
                                         "breakfast": "Breakfast included" if breakfast else "Room only"},
                             rating=round(_f(hotel, "rt", lo=3.7, hi=4.7), 1),
                             link=_src_link(src, {"city": city, "checkin": ci.isoformat(), "checkout": co.isoformat(),
                                                  "adults": q.qty, "rooms": q.rooms}),
                             details={"checkin": ci.isoformat(), "checkout": co.isoformat(), "nights": nights,
                                      "stars": stars}))
    return out


# ------------------------------------------------------------------ buses

_OPERATORS = ["Sahyadri Travels", "Konkan Express Travels", "Deccan Sleeper Lines", "CityLink SmartBus",
              "Western Ghats Volvo"]
_BUS_TYPES = [("AC Sleeper (2+1)", 1.25), ("AC Seater (2+2)", 1.0), ("Non-AC Sleeper", 0.85)]


def bus_offers(q: OfferQuery, today: date) -> list[Offer]:
    if not q.origin or not q.destination:
        return []
    a, b = airport(q.origin), airport(q.destination)
    km = _km(a, b) * 1.25 if a and b else _f(q.origin, q.destination, lo=150, hi=600)
    day = q.day or today
    out = []
    for op in _OPERATORS:
        bt, mult = _BUS_TYPES[_h(op, q.origin, q.destination) % 3]
        if q.class_pref and not set(re.findall(r"[a-z]+", q.class_pref.lower())) <= set(re.findall(r"[a-z]+", bt.lower())):
            continue
        dep = time((18 + _h(op, day) % 7) % 24, (_h(op, "m") % 4) * 15)
        dur = int(km / 50 * 60)
        when = datetime.combine(day, dep, IST)
        seats = _h(op, day, "s") % 30
        for src in ("redbus", "makemytrip"):
            if q.sources and src not in q.sources:
                continue
            unit = _r((150 + km * 1.6) * mult * _f(src, op, lo=0.95, hi=1.06) * _drift(src, op, day=day))
            items = unit * q.qty
            out.append(Offer(_offer_id("bus", src, op, day, q.qty), "bus", src, SOURCES[src]["name"], f"{op}|{day}",
                             f"{op} · {bt}", f"{q.origin.title()} {dep:%H:%M} → {q.destination.title()} "
                             f"{(when + timedelta(minutes=dur)):%H:%M} ({seats} seats left)", q.qty, unit,
                             PriceBreakdown(items_minor=items, taxes_minor=int(items * 0.05)), when=when.isoformat(),
                             duration_min=dur, conditions={"cancellation": "Free until 12 h before departure; 50% after"},
                             rating=round(_f(op, "rt", lo=3.8, hi=4.6), 1),
                             link=_src_link(src, {"origin": q.origin, "destination": q.destination}),
                             details={"operator": op, "bus_type": bt, "seats_left": seats}))
    return out


# ------------------------------------------------------------------ events & shows

_EVENTS = [("Stand-up Comedy Night", "comedy", 499), ("Bollywood Retro Live Concert", "music", 999),
           ("Sufi Evening Under the Stars", "music", 799), ("Marathi Natak: Sahi Re Sahi", "theatre", 599),
           ("Kids Science Fun Workshop", "kids", 399), ("Indie Music Open Mic", "music", 299)]
_TIERS = [("Silver", 1.0), ("Gold", 1.6), ("Platinum", 2.4)]


def event_offers(q: OfferQuery, today: date) -> list[Offer]:
    city = (q.city or "Pune").title()
    want = _tokens(q.query)
    out = []
    for name, kind, base in _EVENTS:
        if want and not (want & _tokens(name + " " + kind)) and not ({"event", "events", "show", "shows"} & want):
            continue
        day = q.day or today + timedelta(days=1 + _h(name, city) % 10)
        when = datetime.combine(day, time(19 + _h(name) % 3, 0), IST)
        for tier, mult in _TIERS:
            if q.class_pref and tier.lower() not in q.class_pref.lower():
                continue
            for src in ("bookmyshow", "district"):
                if q.sources and src not in q.sources:
                    continue
                unit = _r(base * mult * _drift(src, name, day=today))
                items = unit * q.qty
                fee = int(items * (0.06 if src == "bookmyshow" else 0.05))
                out.append(Offer(_offer_id("event", src, name, tier, day, q.qty), "event", src, SOURCES[src]["name"],
                                 f"{name}|{tier}|{day}", f"{name} · {tier}", f"{city} · {when:%a %d %b, %I:%M %p}",
                                 q.qty, unit, PriceBreakdown(items_minor=items, fees_minor={"booking": fee},
                                                             taxes_minor=int(fee * 0.18)),
                                 when=when.isoformat(), conditions={"cancellation": "Non-refundable"},
                                 link=_src_link(src, {"city": city}), details={"event": name, "tier": tier,
                                                                               "kind": kind}))
    return out


# ------------------------------------------------------------------ restaurant tables (best discount wins)

_DINEIN = ["The Terrace Grill", "Malabar Spice Court", "Olive & Basil", "Barbeque Nation-style Grill House",
           "Copper Pot Fine Dine"]


def table_offers(q: OfferQuery, today: date) -> list[Offer]:
    day = q.day or today
    hh = 20
    if q.time_pref and (m := re.search(r"(\d{1,2})(?::(\d{2}))?\s*(pm|am)?", q.time_pref.lower())):
        hh = int(m.group(1)) + (12 if (m.group(3) == "pm" or (not m.group(3) and int(m.group(1)) < 12)) and
                                int(m.group(1)) < 12 else 0)
    slot = datetime.combine(day, time(min(hh, 23), 0), IST)
    want = _tokens(q.query)
    out = []
    for rest in _DINEIN:
        if want - {"table", "book", "reserve", "dinner", "lunch", "restaurant"} and not (want & _tokens(rest)):
            if not ({"table", "dinner", "lunch", "restaurant"} & want):
                continue
        if _h(rest, slot) % 5 == 0:
            continue  # fully booked at that time
        avg = _f(rest, lo=800, hi=2200)
        for src in ("swiggy_dineout", "district"):
            if q.sources and src not in q.sources:
                continue
            pct = [10, 15, 20, 25][_h(src, rest, day) % 4]
            est = _r(avg * q.qty)
            out.append(Offer(_offer_id("table", src, rest, slot, q.qty), "table", src, SOURCES[src]["name"],
                             f"{rest}|{slot}", f"{rest} · table for {q.qty}", f"{slot:%a %d %b, %I:%M %p} · "
                             f"{pct}% off the bill", q.qty, 0, PriceBreakdown(items_minor=0), when=slot.isoformat(),
                             conditions={"discount": f"{pct}% off the bill (est. saving {rupees(est * pct // 100)} on "
                                                     f"~{rupees(est)})", "cancellation": "Free cancellation"},
                             rating=round(_f(rest, "rt", lo=4.0, hi=4.7), 1), link=_src_link(src, {}),
                             details={"restaurant": rest, "discount_pct": pct, "est_bill_minor": est}))
    return out


# ------------------------------------------------------------------ rides (fare estimates)

_RIDE_TYPES = {"uber": [("Uber Moto", 0.6), ("Uber Auto", 0.8), ("Uber Go", 1.0), ("Uber Premier", 1.35)],
               "ola": [("Ola Bike", 0.58), ("Ola Auto", 0.78), ("Ola Mini", 0.97), ("Ola Prime Sedan", 1.3)],
               "rapido": [("Rapido Bike", 0.55), ("Rapido Auto", 0.75), ("Rapido Cab Economy", 0.95)]}
_RIDE_CLASS = {"bike": ("moto", "bike"), "auto": ("auto",), "cab": ("go", "mini", "economy", "premier", "sedan"),
               "sedan": ("premier", "sedan")}


def ride_offers(q: OfferQuery, today: date) -> list[Offer]:
    pickup, drop = q.origin or q.area, q.destination
    if not pickup or not drop:
        return []
    km = round(_f(pickup.lower(), drop.lower(), lo=4, hi=28), 1)
    now = datetime.now(IST)
    surge = 1.2 if 8 <= now.hour <= 10 or 18 <= now.hour <= 21 else 1.0
    out = []
    for src, types in _RIDE_TYPES.items():
        if q.sources and src not in q.sources:
            continue
        for name, mult in types:
            if q.class_pref:
                keys = _RIDE_CLASS.get(q.class_pref.lower().strip(), (q.class_pref.lower(),))
                if not any(k in name.lower() for k in keys):
                    continue
            fare = _r((40 + km * 15) * mult * surge * _f(src, name, pickup, lo=0.93, hi=1.07))
            eta = int(_f(src, name, now.hour, lo=2, hi=9))
            kind = next((k for k, v in _RIDE_CLASS.items() if any(x in name.lower() for x in v)), "cab")
            out.append(Offer(_offer_id("ride", src, name, pickup, drop, now.date(), now.hour), "ride", src,
                             SOURCES[src]["name"], f"{kind}|{pickup}|{drop}", name,
                             f"{pickup.title()} → {drop.title()} · ~{km} km · pickup in {eta} min", 1, fare,
                             PriceBreakdown(items_minor=fare), eta=f"pickup in {eta} min", duration_min=eta,
                             conditions={"fare": "Estimate; final fare may change with route/waiting"
                                         + (" · surge pricing" if surge > 1 else ""),
                                         "cancellation": "Cancellation fee may apply after driver is assigned"},
                             link=_src_link(src, {"pickup": pickup, "dropoff": drop}),
                             details={"km": km, "kind": kind}))
    return out


# ------------------------------------------------------------------ search + rank


def search(q: OfferQuery, now: datetime | None = None) -> tuple[list[Offer], dict[str, Any]]:
    """(offers ranked, meta). meta may ask for a product choice when the query is ambiguous."""
    today = (now or datetime.now(IST)).astimezone(IST).date()
    meta: dict[str, Any] = {}
    if q.vertical == "product":
        matches = match_products(q.query)
        if not matches:
            return [], {"reason": "no_match"}
        if len(matches) > 1 and matches[0][0] - matches[1][0] < 0.15:
            return [], {"ambiguous": [m for _s, m in matches[:4]]}
        offers = product_offers(q, matches[0][1], today)
        meta["product"] = matches[0][1]
    elif q.vertical == "food":
        offers = food_offers(q, today)
    elif q.vertical == "flight":
        offers = flight_offers(q, today)
    elif q.vertical == "hotel":
        offers = hotel_offers(q, today)
    elif q.vertical == "bus":
        offers = bus_offers(q, today)
    elif q.vertical == "event":
        offers = event_offers(q, today)
    elif q.vertical == "table":
        offers = table_offers(q, today)
    elif q.vertical == "ride":
        offers = ride_offers(q, today)
    else:
        offers = []
    if q.time_pref and q.vertical in ("flight", "bus"):
        win = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 21), "night": (21, 29)}.get(
            q.time_pref.lower().strip())
        if win:
            offers = [o for o in offers if o.when and win[0] <= datetime.fromisoformat(o.when).hour + (
                24 if datetime.fromisoformat(o.when).hour < 5 else 0) < win[1]] or offers
    if q.budget_max_minor:
        offers = [o for o in offers if o.total <= q.budget_max_minor] or offers
    return rank(offers, q.sort), meta


def rank(offers: list[Offer], sort: str = "cheapest") -> list[Offer]:
    if sort == "fastest":
        return sorted(offers, key=lambda o: (o.duration_min or 10**6, o.total))
    if sort == "earliest":
        return sorted(offers, key=lambda o: (o.when or "", o.total))
    if sort == "best_rated":
        return sorted(offers, key=lambda o: (-(o.rating or 0), o.total))
    if offers and offers[0].vertical == "table":
        return sorted(offers, key=lambda o: (-o.details.get("discount_pct", 0), -(o.rating or 0)))
    return sorted(offers, key=lambda o: (o.total, o.duration_min or 0))


def savings(offers: list[Offer]) -> dict[str, Any]:
    """How much the best offer saves vs the same item elsewhere and vs the priciest option shown."""
    if not offers:
        return {}
    best = offers[0]
    same = [o.total for o in offers if o.item_key == best.item_key and o is not best]
    return {"best_id": best.id, "vs_same_item_elsewhere_minor": (min(same) - best.total) if same else 0,
            "vs_most_expensive_minor": max(o.total for o in offers) - best.total,
            "sources_compared": sorted({o.source_name for o in offers})}


def current_best(q: OfferQuery, item_key: str | None, now: datetime | None = None) -> Offer | None:
    """Cheapest offer today for the same item (price-drop watches)."""
    offers, _meta = search(q, now)
    if item_key:
        offers = [o for o in offers if o.item_key == item_key]
    return offers[0] if offers else None
