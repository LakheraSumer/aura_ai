"""Rail search provider contract + the official-link fallback.

No authorised Indian Railways availability API is configured in this environment, so the only
implementation is `OfficialLinkRailProvider`: it resolves stations from a small curated table
and returns the official IRCTC search page. It NEVER returns trains, fares, seats or PNRs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

# City -> major stations (code, name). Curated subset; a full station master comes from an
# authorised data source later (docs/integrations/TRAVEL_INTEGRATIONS.md).
STATIONS: dict[str, list[tuple[str, str]]] = {
    "pune": [("PUNE", "Pune Jn")],
    "mumbai": [("CSMT", "Mumbai CSMT"), ("MMCT", "Mumbai Central"), ("LTT", "Lokmanya Tilak Terminus"),
               ("DR", "Dadar"), ("BDTS", "Bandra Terminus")],
    "delhi": [("NDLS", "New Delhi"), ("DLI", "Old Delhi"), ("NZM", "Hazrat Nizamuddin"), ("ANVT", "Anand Vihar")],
    "bengaluru": [("SBC", "KSR Bengaluru"), ("YPR", "Yesvantpur Jn"), ("SMVB", "SMVT Bengaluru")],
    "chennai": [("MAS", "MGR Chennai Central"), ("MS", "Chennai Egmore")],
    "kolkata": [("HWH", "Howrah Jn"), ("SDAH", "Sealdah"), ("KOAA", "Kolkata")],
    "hyderabad": [("SC", "Secunderabad Jn"), ("HYB", "Hyderabad Deccan"), ("KCG", "Kacheguda")],
    "ahmedabad": [("ADI", "Ahmedabad Jn")],
    "nagpur": [("NGP", "Nagpur Jn")],
    "nashik": [("NK", "Nashik Road")],
    "surat": [("ST", "Surat")],
    "jaipur": [("JP", "Jaipur Jn")],
    "lucknow": [("LKO", "Lucknow Charbagh"), ("LJN", "Lucknow Jn")],
    "goa": [("MAO", "Madgaon Jn"), ("VSG", "Vasco da Gama")],
}
ALIASES: dict[str, str] = {
    "bombay": "mumbai", "मुंबई": "mumbai", "मुम्बई": "mumbai", "bangalore": "bengaluru", "बेंगलुरु": "bengaluru",
    "पुणे": "pune", "poona": "pune", "दिल्ली": "delhi", "new delhi": "delhi", "calcutta": "kolkata",
    "कोलकाता": "kolkata", "madras": "chennai", "चेन्नई": "chennai", "हैदराबाद": "hyderabad", "नागपुर": "nagpur",
    "नाशिक": "nashik", "अहमदाबाद": "ahmedabad", "जयपुर": "jaipur", "लखनऊ": "lucknow",
}

IRCTC_SEARCH_URL = "https://www.irctc.co.in/nget/train-search"


def normalise_city(word: str | None) -> str | None:
    if not word:
        return None
    w = word.strip().lower()
    w = ALIASES.get(w, w)
    return w if w in STATIONS else None


def find_cities(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    t = text.lower()
    for name in list(STATIONS) + list(ALIASES):
        for m in re.finditer(rf"(?<![\wऀ-ॿ]){re.escape(name)}(?![\wऀ-ॿ])", t):
            city = normalise_city(name)
            if city:
                found.append((m.start(), city))
    seen: list[str] = []
    for _, c in sorted(found):
        if c not in seen:
            seen.append(c)
    return seen


@dataclass
class RailSearchResult:
    provider: str
    environment: str
    data_available: bool
    origin_stations: list[dict[str, str]]
    destination_stations: list[dict[str, str]]
    travel_date: str
    travel_class: str | None
    time_preference: str | None
    official_link: str
    trains: list[dict] = field(default_factory=list)
    notice: str = ""


class RailProvider(Protocol):
    name: str

    async def search(self, origin: str, destination: str, travel_date: str, travel_class: str | None,
                     time_preference: str | None) -> RailSearchResult: ...


class OfficialLinkRailProvider:
    name = "irctc-official-link"

    async def search(self, origin: str, destination: str, travel_date: str, travel_class: str | None,
                     time_preference: str | None) -> RailSearchResult:
        o, d = normalise_city(origin), normalise_city(destination)
        if o is None or d is None:
            raise ValueError("unknown station")
        return RailSearchResult(
            provider=self.name,
            environment="official-link-fallback",
            data_available=False,
            origin_stations=[{"code": c, "name": n} for c, n in STATIONS[o]],
            destination_stations=[{"code": c, "name": n} for c, n in STATIONS[d]],
            travel_date=travel_date,
            travel_class=travel_class,
            time_preference=time_preference,
            official_link=IRCTC_SEARCH_URL,
            notice=("No authorised train-availability source is connected, so no trains, fares or seat "
                    "counts are shown. Use the official IRCTC search with these stations and date."),
        )
