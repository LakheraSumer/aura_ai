"""Web search for news, IPOs, prices and general questions. LIVE.

- Tavily Search API when TAVILY_API_KEY is set (free plan: 1,000 credits/month, no card).
- Otherwise free sources: Google News RSS (news, IPOs, prices; verified 2026-11-16) and the Wikipedia
  REST API (general topics).

Results are untrusted web data: they are shown to the user as titles + links + dates and are never fed
back to the planner, so a page can't instruct the assistant. Nothing here is invented; when a source
returns nothing, the reply says so.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from apna.core.config import Settings

TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject httpx.MockTransport
_UA = {"User-Agent": "ApnaAssistant/0.1 (personal assistant; test deployment)"}
_MAX_BYTES = 2_000_000

# Live IPO subscription / allotment / GMP figures need exchange data licensing; we link the official pages.
IPO_OFFICIAL = [("NSE upcoming issues", "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"),
                ("BSE public issues", "https://www.bseindia.com/publicissue.html")]


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    source: str
    snippet: str = ""
    published: str | None = None  # ISO date


@dataclass(frozen=True)
class WebAnswer:
    provider: str
    results: list[WebResult]
    summary: str | None = None  # Tavily's own answer, labelled as such


def _clean(s: str | None, n: int = 300) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()[:n]


async def _client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=12, transport=TRANSPORT, headers=_UA, follow_redirects=True)


async def tavily(settings: Settings, query: str, topic: str) -> WebAnswer:
    assert settings.tavily_api_key is not None
    async with await _client(settings) as c:
        r = await c.post(f"{settings.tavily_api_base_url}/search",
                         headers={"Authorization": f"Bearer {settings.tavily_api_key.get_secret_value()}"},
                         json={"query": query, "topic": "news" if topic in ("news", "ipo") else "general",
                               "max_results": 5, "include_answer": True, "country": "india"})
    r.raise_for_status()
    data = r.json()
    res = [WebResult(_clean(x.get("title"), 160), str(x.get("url", "")), _domain(str(x.get("url", ""))),
                     _clean(x.get("content")), (x.get("published_date") or None)) for x in data.get("results") or []]
    return WebAnswer("Tavily", [x for x in res if x.url.startswith("http")], _clean(data.get("answer"), 700) or None)


def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else ""


async def google_news(settings: Settings, query: str, limit: int = 5) -> list[WebResult]:
    url = f"{settings.google_news_rss_url}?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    async with await _client(settings) as c:
        r = await c.get(url)
    r.raise_for_status()
    if len(r.content) > _MAX_BYTES:
        return []
    root = ET.fromstring(r.content)
    out = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"), 200)
        src = _clean(item.findtext("source"), 60)
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        pub = item.findtext("pubDate")
        try:
            iso = parsedate_to_datetime(pub).date().isoformat() if pub else None
        except (TypeError, ValueError):
            iso = None
        link = (item.findtext("link") or "").strip()
        if title and link.startswith("http"):
            out.append(WebResult(title, link, src or "Google News", published=iso))
        if len(out) >= limit:
            break
    return out


async def wikipedia(settings: Settings, query: str) -> WebResult | None:
    async with await _client(settings) as c:
        s = await c.get(f"{settings.wikipedia_base_url}/w/api.php",
                        params={"action": "query", "list": "search", "srsearch": query, "srlimit": 1,
                                "format": "json"})
        s.raise_for_status()
        hits = (s.json().get("query") or {}).get("search") or []
        if not hits:
            return None
        title = hits[0]["title"]
        p = await c.get(f"{settings.wikipedia_base_url}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}")
    if p.status_code != 200:
        return None
    d = p.json()
    url = ((d.get("content_urls") or {}).get("desktop") or {}).get("page") or \
        f"{settings.wikipedia_base_url}/wiki/{quote(title.replace(' ', '_'))}"
    return WebResult(_clean(d.get("title") or title, 160), url, "Wikipedia", _clean(d.get("extract"), 600))


def classify(query: str) -> str:
    q = query.lower()
    if re.search(r"\bipo\b|listing|gmp|allotment|subscription status", q):
        return "ipo"
    if re.search(r"news|khabar|samachar|latest|today|aaj|update|headlines|price|rate|bhav|score|result", q):
        return "news"
    return "general"


async def search(settings: Settings, query: str, kind: str | None = None) -> WebAnswer:
    kind = kind or classify(query)
    q = query if kind != "ipo" or "ipo" in query.lower() else f"{query} IPO"
    if settings.tavily_configured():
        try:
            return await tavily(settings, q, kind)
        except (httpx.HTTPError, ValueError):
            pass  # fall back to the free sources below rather than failing the user's question
    results: list[WebResult] = []
    if kind in ("news", "ipo"):
        results = await google_news(settings, q)
    else:
        wiki = await wikipedia(settings, q)
        results = ([wiki] if wiki else []) + await google_news(settings, q, limit=3)
    return WebAnswer("Google News + Wikipedia" if kind == "general" else "Google News", results)


def to_dict(a: WebAnswer) -> dict[str, Any]:
    return {"provider": a.provider, "summary": a.summary,
            "results": [{"title": r.title, "url": r.url, "source": r.source, "snippet": r.snippet,
                         "published": r.published} for r in a.results]}
