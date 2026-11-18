"""OAuth redirect landing page (public, served by the webhook-only instance): finishes "connect Google".

The user signed in on Google's own page; this page only receives the one-time code and state. Tokens are
exchanged server-side and stored encrypted; nothing sensitive is shown or logged.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from apna.api.deps import DB
from apna.core.config import get_settings
from apna.core.errors import ApnaError
from apna.core.logging import log
from apna.providers import google

router = APIRouter(prefix="/v1/oauth", tags=["oauth"])

_PAGE = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
         "initial-scale=1'><meta name='referrer' content='no-referrer'><title>Apna AI · Google</title><style>"
         "body{{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px;color:#111}}"
         "</style></head><body>{body}</body></html>")


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@router.get("/google/callback", response_class=HTMLResponse)
async def google_callback(request: Request, db: DB, state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    if error:
        return _page(f"<h2>Not connected</h2><p>Google said: {html.escape(error[:80])}. Nothing was stored.</p>", 400)
    if not (state and code) or not get_settings().google_configured():
        return _page("<h2>Not connected</h2><p>This link is incomplete or Google isn't set up.</p>", 400)
    try:
        user_id = await google.finish_connect(db, get_settings(), state, code)
    except ApnaError as exc:
        await db.commit()  # keep the state marked as used
        return _page(f"<h2>Not connected</h2><p>{html.escape(exc.message)}</p>", 400)
    await db.commit()
    log.info("google_connected")
    conv = getattr(request.app.state, "conversations", None)
    if conv is not None:
        await conv._system_reply(db, user_id, "✅ Google connected. Try \"show my latest emails\" or \"what's on my "
                                             "calendar tomorrow?\". Say \"disconnect Google\" any time.",
                                 f"google-connected:{state[:16]}")
        await db.commit()
    return _page("<h2>Google connected ✅</h2><p>You can close this page and go back to the chat.</p>")
