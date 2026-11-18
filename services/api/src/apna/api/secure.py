"""One-time secure entry page for provider login codes (OTP). Never used for payment secrets.

Served by the public webhook-only instance. The code is posted straight to this page, encrypted, and
consumed once by the provider adapter: it never passes through chat, the planner, logs or workflow
history. The page shows which provider is asking and why.
"""

from __future__ import annotations

import html
from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from apna.api.deps import DB
from apna.core.errors import ApnaError
from apna.security import otp

router = APIRouter(prefix="/v1/secure", tags=["secure"])

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,
initial-scale=1"><meta name="referrer" content="no-referrer"><title>Apna AI · secure code</title>
<style>body{{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px;color:#111}}
.box{{border:1px solid #ddd;border-radius:12px;padding:20px}}input{{font-size:20px;padding:10px;width:100%;
box-sizing:border-box;letter-spacing:4px}}button{{margin-top:12px;font-size:18px;padding:10px;width:100%}}
.muted{{color:#555;font-size:14px}}</style></head><body><div class="box">{body}</div></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@router.get("/challenge/{token}", response_class=HTMLResponse)
async def show_challenge(token: str, db: DB) -> HTMLResponse:
    try:
        ch = await otp.open_challenge(db, token)
    except ApnaError as exc:
        return _page(f"<p>{html.escape(exc.message)}</p>", exc.http_status)
    return _page(
        f"<h2>Code for {html.escape(ch.provider)}</h2>"
        f"<p>{html.escape(ch.provider)} sent you a code to <b>{html.escape(ch.purpose)}</b>. Enter it here, not in chat.</p>"
        f"<form method='post'><input name='code' inputmode='numeric' autocomplete='one-time-code' "
        f"pattern='[0-9]{{4,8}}' required autofocus><button type='submit'>Send code</button></form>"
        f"<p class='muted'>Never enter a UPI PIN, card PIN, CVV or bank password here. This link works once and "
        f"expires in a few minutes.</p>")


@router.post("/challenge/{token}", response_class=HTMLResponse)
async def submit_challenge(token: str, code: Annotated[str, Form()], db: DB) -> HTMLResponse:
    try:
        await otp.submit_code(db, token, code)
    except ApnaError as exc:
        await db.commit()  # keep the attempt count even when the code is rejected
        return _page(f"<p>{html.escape(exc.message)}</p>", exc.http_status)
    return _page("<h2>Code received ✅</h2><p>You can close this page and go back to the chat.</p>")
