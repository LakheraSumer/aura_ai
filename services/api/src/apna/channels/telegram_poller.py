"""Telegram long polling: the server fetches messages itself, so it needs no public URL, tunnel or website.

    python -m apna.channels.telegram_poller

Use this OR the webhook, not both: Telegram refuses getUpdates while a webhook is set, so this process
removes the webhook when it starts. Updates are confirmed (offset) only after they were handled; a crash
re-delivers them and the inbound dedupe key (`telegram:<update_id>`) drops the duplicate.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from temporalio.client import Client

from apna.channels import telegram
from apna.core.config import get_settings
from apna.core.errors import ApnaError, Conflict
from apna.core.logging import configure_logging, log
from apna.db.session import session_scope
from apna.services.conversation import ConversationService

POLL_TIMEOUT = 50  # seconds Telegram holds the request open when there is nothing new
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject httpx.MockTransport


async def handle_update(conv: ConversationService, http: httpx.AsyncClient, base: str, update: dict[str, Any]) -> int:
    handled = 0
    for ev in telegram.parse_update(update):
        try:
            async with session_scope() as db:
                await conv.handle_inbound(db, ev)
            handled += 1
        except Conflict:
            pass  # duplicate delivery or stale button: nothing to do
        except ApnaError as exc:
            log.warning("inbound_rejected", channel="telegram", code=exc.code)
    if ack := telegram.callback_ack(update):  # stop the button's loading spinner
        method = ack.pop("method")
        try:
            await http.post(f"{base}/{method}", json=ack)
        except httpx.HTTPError:
            pass  # cosmetic only
    return handled


async def run(stop: asyncio.Event | None = None) -> None:
    s = get_settings()
    if not (s.telegram_enabled and s.telegram_bot_token):
        raise SystemExit("TELEGRAM_ENABLED=true and TELEGRAM_BOT_TOKEN are required")
    base = f"{s.telegram_api_base_url}/bot{s.telegram_bot_token.get_secret_value()}"
    client = await Client.connect(s.temporal_address, namespace=s.temporal_namespace)
    conv = ConversationService(client)
    stop = stop or asyncio.Event()
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 15, transport=TRANSPORT) as http:
        r = await http.post(f"{base}/deleteWebhook", json={"drop_pending_updates": False})
        log.info("telegram_polling_started", webhook_removed=r.json().get("ok") if r.content else False)
        offset: int | None = None
        backoff = 1.0
        while not stop.is_set():
            try:
                body: dict[str, Any] = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    body["offset"] = offset
                resp = await http.post(f"{base}/getUpdates", json=body)
                data = resp.json()
                if not data.get("ok"):
                    raise httpx.HTTPError(f"getUpdates {resp.status_code}: {data.get('description')}")
                for update in data.get("result") or []:
                    await handle_update(conv, http, base, update)
                    offset = int(update["update_id"]) + 1
                backoff = 1.0
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telegram_poll_error", error=str(exc)[:200], retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    configure_logging()
    asyncio.run(run())
