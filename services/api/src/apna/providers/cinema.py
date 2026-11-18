"""Sandbox cinema: a TEST stand-in for a ticketing partner API (status: WORKING_IN_MOCK).

Real BookMyShow booking needs their partner API (business agreement); their site blocks bots and needs
the user's login and payment, so Apna never automates it. This provider gives the chat flow everything
a partner API would (theatres, shows, seat maps, holds, payment, booking) so the full conversation can be
built and tested. Nothing here is real: seat availability is simulated, payment is a test payment and
booking IDs start with TEST-. Every reply says so and links to the real BookMyShow page.

Theatre names and prices for Pune mirror what users see on BookMyShow (e.g. City Pride: Kothrud, Royal
Gold rows A-M at ₹490, Royal Silver N-O at ₹460), but availability is NOT real.

Pure functions (layout, simulated sales, seat choice, matching) are separate from the DB functions
(holds and bookings) so the selection logic is unit-tested without a database.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

IST = ZoneInfo("Asia/Kolkata")
HOLD_MINUTES = 15
MERCHANT = "Sandbox Cinemas (TEST)"


@dataclass(frozen=True)
class SeatClass:
    name: str
    price_minor: int  # paise
    rows: tuple[str, ...]
    seats_per_row: int


@dataclass(frozen=True)
class Theatre:
    id: str
    name: str
    area: str
    classes: tuple[SeatClass, ...]
    times: tuple[str, ...]  # "HH:MM" 24h, IST


def _rows(a: str, b: str) -> tuple[str, ...]:
    return tuple(chr(c) for c in range(ord(a), ord(b) + 1))


_PUNE = (
    Theatre("city-pride-kothrud", "City Pride: Kothrud", "Kothrud",
            (SeatClass("Royal Gold", 49000, _rows("A", "M"), 22), SeatClass("Royal Silver", 46000, ("N", "O"), 22)),
            ("16:00", "19:00", "22:45")),
    Theatre("pvr-pavillion", "PVR: Pavillion Mall, SB Road", "Shivajinagar",
            (SeatClass("Prime", 45000, _rows("A", "J"), 20), SeatClass("Recliner", 75000, ("K", "L"), 10)),
            ("13:30", "18:15", "21:40")),
    Theatre("cinepolis-seasons", "Cinepolis: Seasons Mall, Hadapsar", "Hadapsar",
            (SeatClass("Classic", 38000, _rows("A", "K"), 18), SeatClass("Recliner", 65000, ("L", "M"), 10)),
            ("15:00", "19:30", "22:30")),
    Theatre("inox-bund-garden", "INOX: Bund Garden Road", "Bund Garden",
            (SeatClass("Gold", 42000, _rows("A", "L"), 20),),
            ("12:00", "17:45", "21:00")),
)

# Shows that are always sold out, so "housefull -> other shows / other theatres" can be exercised.
_HOUSEFULL = {("city-pride-kothrud", "19:00")}


def theatres_for(city: str | None) -> list[Theatre]:
    c = (city or "").strip()
    if not c:
        return []
    if c.lower() in {"pune", "poona", "पुणे"}:
        return list(_PUNE)
    slug = re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-") or "city"
    name = c.title()
    return [
        Theatre(f"sandbox-{slug}-central", f"Sandbox Cinema: {name} Central", "Central",
                (SeatClass("Gold", 30000, _rows("A", "J"), 16),), ("14:00", "18:00", "21:30")),
        Theatre(f"sandbox-{slug}-mall", f"Sandbox Cinema: {name} Mall", "Mall",
                (SeatClass("Classic", 25000, _rows("A", "H"), 14), SeatClass("Recliner", 50000, ("I",), 8)),
                ("16:30", "20:00")),
    ]


def show_id(th: Theatre, day: date, hhmm: str) -> str:
    return f"{th.id}:{day:%Y%m%d}:{hhmm.replace(':', '')}"


def show_start(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), IST)


def all_seats(th: Theatre) -> dict[str, tuple[SeatClass, list[int]]]:
    return {row: (cls, list(range(1, cls.seats_per_row + 1))) for cls in th.classes for row in cls.rows}


def presold(th: Theatre, sid: str, hhmm: str) -> set[str]:
    """Simulated sales: deterministic per show, busier in the evening; some shows are housefull."""
    if (th.id, hhmm) in _HOUSEFULL:
        return {f"{r}{n}" for r, (_c, nums) in all_seats(th).items() for n in nums}
    hour = int(hhmm[:2])
    occupancy = 0.35 if hour < 17 else 0.6 if hour < 21 else 0.45
    sold = set()
    for row, (_cls, nums) in all_seats(th).items():
        row_occ = occupancy + (0.25 if row in ("D", "E", "F") else 0.0)  # middle rows go first
        for n in nums:
            h = int(hashlib.sha256(f"{sid}:{row}{n}".encode()).hexdigest()[:6], 16) % 1000
            if h < row_occ * 1000:
                sold.add(f"{row}{n}")
    return sold


def free_by_row(th: Theatre, taken: set[str]) -> dict[str, list[int]]:
    return {row: [n for n in nums if f"{row}{n}" not in taken] for row, (_c, nums) in all_seats(th).items()}


def adjacent_blocks(free: list[int], n: int, row_len: int) -> list[list[int]]:
    """All windows of n consecutive free seats, closest to the row centre first."""
    s = set(free)
    blocks = [list(range(a, a + n)) for a in free if all(a + i in s for i in range(n))]
    centre = (row_len + 1) / 2
    return sorted(blocks, key=lambda b: abs((b[0] + b[-1]) / 2 - centre))


@dataclass
class SeatPick:
    row: str
    seats: list[int]
    cls: SeatClass

    @property
    def labels(self) -> list[str]:
        return [f"{self.row}{n}" for n in self.seats]


def _class_named(th: Theatre, name: str | None) -> SeatClass | None:
    if not name:
        return None
    n = name.lower()
    for cls in th.classes:
        if cls.name.lower() == n or n in cls.name.lower() or cls.name.lower().split()[-1] == n:
            return cls
    return None


def choose_seats(th: Theatre, taken: set[str], n: int, rows: list[str] | None = None,
                 seat_class: str | None = None) -> tuple[SeatPick | None, list[SeatPick]]:
    """(pick, alternatives). Seats are always together in one row. Preferred rows first; otherwise the
    best rows (middle of the hall). Alternatives = nearest other rows that can seat everyone together."""
    layout = all_seats(th)
    order = list(layout)  # A.. front to back
    free = free_by_row(th, taken)
    want_rows = [r.upper() for r in (rows or []) if r and r.upper() in layout]
    cls = _class_named(th, seat_class)

    def best_in(row: str) -> SeatPick | None:
        rcls, nums = layout[row]
        blocks = adjacent_blocks(free[row], n, len(nums))
        return SeatPick(row, blocks[0], rcls) if blocks else None

    if want_rows:
        for r in want_rows:
            if p := best_in(r):
                return p, []
        ref = min(order.index(r) for r in want_rows)
        same_cls = {layout[r][0].name for r in want_rows}
        others = sorted((r for r in order if r not in want_rows), key=lambda r: abs(order.index(r) - ref))
        alts = [p for r in others if (p := best_in(r)) and (p.cls.name in same_cls or len(same_cls) == 0)]
        if len(alts) < 3:  # fill with any class if the preferred class has too few
            alts += [p for r in others if (p := best_in(r)) and p.row not in {a.row for a in alts}]
        return None, alts[:3]

    candidates = [r for r in order if cls is None or layout[r][0] == cls]
    mid = 0.6 * (len(candidates) - 1)
    for r in sorted(candidates, key=lambda r: abs(candidates.index(r) - mid)):
        if p := best_in(r):
            return p, []
    return None, []


def match_theatre(hint: str | None, theatres: list[Theatre]) -> Theatre | None:
    """'citipride kothrud' -> City Pride: Kothrud; 'pvr sb road' -> PVR: Pavillion Mall, SB Road."""
    if not hint:
        return None

    def compact(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    h, words = compact(hint), [w for w in re.split(r"[^a-z0-9]+", hint.lower()) if len(w) > 2]
    best, best_score = None, 0.0
    for th in theatres:
        target = compact(th.name + th.area)
        score = difflib.SequenceMatcher(None, h, compact(th.name)).ratio()
        score += 0.4 * sum(1 for w in words if w in target or difflib.get_close_matches(w, re.split(
            r"[^a-z0-9]+", (th.name + " " + th.area).lower()), n=1, cutoff=0.8))
        if score > best_score:
            best, best_score = th, score
    return best if best_score >= 0.6 else None


def match_show(text: str | None, times: list[str]) -> str | None:
    """'10:45 pm' / '10:45' / '7 baje' / 'evening' / 'night' -> one of the show times, or None."""
    if not text or not times:
        return None
    from apna.agent.language import resolve_time

    t = resolve_time(text)
    cands: list[time] = []
    if t.value:
        cands = [t.value]
    elif t.ambiguity and t.ambiguity.question_key == "am_pm_ambiguous":
        cands = [time.fromisoformat(o) for o in t.ambiguity.options]
    if cands:
        def gap(hhmm: str) -> int:
            st = time.fromisoformat(hhmm)
            return min(abs((st.hour * 60 + st.minute) - (c.hour * 60 + c.minute)) for c in cands)
        best = min(times, key=gap)
        return best if gap(best) <= 45 else None
    window = {"morning": (6, 12), "afternoon": (12, 17), "evening": (17, 21), "night": (21, 24)}.get(
        t.period_only or "")
    if window:
        inside = [x for x in times if window[0] <= int(x[:2]) < window[1]]
        return inside[0] if len(inside) == 1 else None
    return None


def fmt_time(hhmm: str) -> str:
    return time.fromisoformat(hhmm).strftime("%I:%M %p").lstrip("0")


def fmt_seats(row: str, seats: list[int]) -> str:
    return ", ".join(f"{row}{n}" for n in seats)


def rupees(minor: int) -> str:
    return f"₹{minor // 100:,}" if minor % 100 == 0 else f"₹{minor / 100:,.2f}"


# ------------------------------------------------------------------ DB: holds and bookings


async def taken_seats(session: AsyncSession, sid: str, *, exclude_hold: uuid.UUID | None = None) -> set[str]:
    from apna.db.models import SandboxCinemaBooking as B

    now = datetime.now(UTC)
    rows = (await session.execute(select(B).where(B.show_id == sid))).scalars()
    taken: set[str] = set()
    for b in rows:
        if b.id == exclude_hold:
            continue
        if b.status == "BOOKED" or (b.status == "HELD" and b.expires_at > now):
            taken.update(b.seats)
    return taken


async def create_hold(session: AsyncSession, *, user_id: uuid.UUID, idempotency_key: str, th: Theatre, sid: str,
                      starts_at: datetime, movie: str, pick: SeatPick):
    from apna.db.models import SandboxCinemaBooking as B

    existing = (await session.execute(select(B).where(B.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing:
        return existing
    hold = B(user_id=user_id, idempotency_key=idempotency_key, show_id=sid, theatre=th.name, movie=movie,
             starts_at=starts_at, seats=pick.labels, seat_class=pick.cls.name,
             amount_minor=pick.cls.price_minor * len(pick.seats), status="HELD",
             expires_at=datetime.now(UTC) + timedelta(minutes=HOLD_MINUTES))
    session.add(hold)
    await session.flush()
    return hold


class SeatsTaken(Exception):
    pass


async def pay_and_book(session: AsyncSession, hold_id: uuid.UUID, user_id: uuid.UUID):
    """Test payment + booking. Idempotent: a booked hold returns the same booking."""
    from apna.db.models import SandboxCinemaBooking as B

    b = (await session.execute(select(B).where(B.id == hold_id).with_for_update())).scalar_one()
    if b.user_id != user_id:
        raise PermissionError("hold belongs to another user")
    if b.status == "BOOKED":
        return b
    if b.status != "HELD" or b.expires_at <= datetime.now(UTC):
        # Hold lapsed while waiting for approval: book only if the same seats are still free.
        taken = await taken_seats(session, b.show_id, exclude_hold=b.id)
        th = next((t for t in _all_known_theatres(b.show_id)), None)
        sold = presold(th, b.show_id, _hhmm_of(b.show_id)) if th else set()
        if set(b.seats) & (taken | sold):
            b.status = "RELEASED"
            raise SeatsTaken(", ".join(sorted(set(b.seats) & (taken | sold))))
    b.status = "BOOKED"
    b.payment_ref = "TESTPAY-" + hashlib.sha256(f"pay:{b.id}".encode()).hexdigest()[:10].upper()
    b.booking_code = "TEST-" + hashlib.sha256(f"book:{b.id}".encode()).hexdigest()[:8].upper()
    b.paid_at = datetime.now(UTC)
    await session.flush()
    return b


async def get_booking(session: AsyncSession, code: str):
    from apna.db.models import SandboxCinemaBooking as B

    return (await session.execute(select(B).where(B.booking_code == code))).scalar_one_or_none()


def _hhmm_of(sid: str) -> str:
    raw = sid.rsplit(":", 1)[-1]
    return f"{raw[:2]}:{raw[2:]}"


def _all_known_theatres(sid: str):
    tid = sid.split(":", 1)[0]
    for th in _PUNE:
        if th.id == tid:
            yield th
    if tid.startswith("sandbox-"):
        city = tid.removeprefix("sandbox-").rsplit("-", 1)[0].replace("-", " ")
        for th in theatres_for(city):
            if th.id == tid:
                yield th
