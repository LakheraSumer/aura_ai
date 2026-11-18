"""Movie tickets: official BookMyShow links only. No scraping, no automated booking.

BookMyShow is behind bot protection (server requests get a Cloudflare challenge), and booking needs the
user's login and payment, so Apna never automates it. It sends the user to BookMyShow's own showtimes
page for the movie, city and date, where they pick a show and pay. It never invents showtimes, prices
or seats.

Link formats, checked in a real browser on 2026-11-14:
- show page  https://in.bookmyshow.com/movies/{city}/{movie-slug}/buytickets/{ET-code}/{YYYYMMDD}
  ("Ramayana Movie Showtimes in Ambajogai/Pune"; works for small towns too)
- city page  https://in.bookmyshow.com/explore/movies-{city}
The ET code is BookMyShow's internal movie ID and is the same in every city. It can't be derived from a
title, so it comes from the `movie_catalog` table: seeded from BookMyShow's listing, plus codes learned
from BookMyShow links that users send. Unknown movie -> city page and a request for the link.
"""

from __future__ import annotations

import difflib
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

BMS_HOME = "https://in.bookmyshow.com/"
BMS_CITY_URL = "https://in.bookmyshow.com/explore/movies-{slug}"
BMS_SHOW_URL = "https://in.bookmyshow.com/movies/{city}/{movie}/buytickets/{et}/{ymd}"

# Cities whose BookMyShow slug is not simply the lower-cased name, plus common other spellings and
# Devanagari names. Any other Latin-script city name is slugified (verified: "ambajogai" works).
_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "mumbai": ("mumbai", "bombay", "मुंबई", "मुम्बई"),
    "navi-mumbai": ("navi mumbai", "नवी मुंबई"),
    "thane": ("thane", "ठाणे"),
    "pune": ("pune", "poona", "पुणे"),
    "national-capital-region-ncr": ("delhi", "new delhi", "delhi ncr", "ncr", "gurgaon", "gurugram",
                                    "दिल्ली", "नई दिल्ली"),
    "noida": ("noida", "नोएडा"),
    "bengaluru": ("bengaluru", "bangalore", "बेंगलुरु", "बंगलौर"),
    "hyderabad": ("hyderabad", "हैदराबाद"),
    "chennai": ("chennai", "madras", "चेन्नई"),
    "kolkata": ("kolkata", "calcutta", "कोलकाता"),
    "ahmedabad": ("ahmedabad", "अहमदाबाद"),
    "vadodara": ("vadodara", "baroda", "वडोदरा"),
    "jaipur": ("jaipur", "जयपुर"),
    "lucknow": ("lucknow", "लखनऊ"),
    "nagpur": ("nagpur", "नागपूर", "नागपुर"),
    "nashik": ("nashik", "nasik", "नाशिक"),
    "aurangabad": ("aurangabad", "sambhajinagar", "chhatrapati sambhajinagar", "औरंगाबाद", "संभाजीनगर"),
    "kolhapur": ("kolhapur", "कोल्हापूर", "कोल्हापुर"),
    "goa": ("goa", "panaji", "गोवा"),
    "kochi": ("kochi", "cochin"),
    "ambajogai": ("ambajogai", "अंबाजोगाई"),
}
_ALIAS = {name: slug for slug, names in _CITY_ALIASES.items() for name in names}
_BMS_LINK = re.compile(r"in\.bookmyshow\.com/movies/([a-z0-9-]+)/([a-z0-9-]+)/(?:buytickets/)?(ET\d{8})", re.I)


def bms_city_slug(city: str | None) -> str | None:
    if not city:
        return None
    c = re.sub(r"\s+", " ", city.strip().lower())
    if c in _ALIAS:
        return _ALIAS[c]
    if re.fullmatch(r"[a-z][a-z .'-]{1,40}", c):  # Latin-script town name -> BookMyShow's slug form
        return re.sub(r"[^a-z]+", "-", c).strip("-")
    return None  # unknown non-Latin spelling: don't guess a URL


def city_page(city: str | None) -> tuple[str, bool]:
    slug = bms_city_slug(city)
    return (BMS_CITY_URL.format(slug=slug), True) if slug else (BMS_HOME, False)


def show_page(city_slug: str, movie_slug: str, et_code: str, day: date) -> str:
    return BMS_SHOW_URL.format(city=city_slug, movie=movie_slug, et=et_code, ymd=day.strftime("%Y%m%d"))


def parse_bms_links(text: str) -> list[tuple[str, str, str]]:
    """(city, movie_slug, ET-code) for every BookMyShow movie link in the text."""
    return [(c.lower(), s.lower(), e.upper()) for c, s, e in _BMS_LINK.findall(text or "")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


async def learn_from_text(session: AsyncSession, text: str) -> int:
    """Remember movie IDs from BookMyShow links the user sends. Returns how many were new."""
    from apna.db.models import MovieCatalog

    new = 0
    for _city, slug, et in parse_bms_links(text):
        row = (await session.execute(select(MovieCatalog).where(MovieCatalog.et_code == et))).scalar_one_or_none()
        if row is None:
            session.add(MovieCatalog(et_code=et, slug=slug, title=slug.replace("-", " ").title(),
                                     source="user-link"))
            new += 1
    if new:
        await session.flush()
    return new


def match_movie(query: str, rows: list):
    """Best match by slug: exact, then close spelling ("ramayan" -> ramayana), then all words contained
    ("jailer 2" -> rajini-the-jailer-2, only if exactly one movie matches). None when unsure."""
    q = _norm(query)
    if not q:
        return None
    by_slug = {r.slug: r for r in rows}
    if q in by_slug:
        return by_slug[q]
    close = difflib.get_close_matches(q, list(by_slug), n=1, cutoff=0.8)
    if close:
        return by_slug[close[0]]
    words = [w for w in q.split("-") if w]
    hits = [r for r in rows if words and all(w in r.slug.split("-") for w in words)]
    return hits[0] if len(hits) == 1 else None


async def find_movie(session: AsyncSession, query: str):
    from apna.db.models import MovieCatalog

    rows = list((await session.execute(select(MovieCatalog))).scalars())  # small table
    return match_movie(query, rows)
