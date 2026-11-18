"""Telegram long polling (server mode, no public URL): messages and button taps flow through the same
conversation service as the webhook; updates are confirmed only after handling; duplicates are dropped."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from apna.channels import telegram_poller
from apna.db.models import LinkedChannel, Task
from apna.db.session import session_scope
from sqlalchemy import select
from test_commerce import scripted  # noqa: F401 - fixture


def _msg(update_id: int, chat_id: int, text: str) -> dict:
    return {"update_id": update_id, "message": {"message_id": update_id, "date": int(time.time()), "text": text,
                                                 "chat": {"id": chat_id, "type": "private"},
                                                 "from": {"id": chat_id, "first_name": "Poll"}}}


async def test_polling_handles_messages_once_and_confirms_offsets(temporal, workers, scripted, monkeypatch):
    chat = 700000000 + int(time.time()) % 100000
    scripted("hello from polling", "answer_question", {"answer": "Namaste!"})
    stop = asyncio.Event()
    calls: list[dict] = []
    batches = [[_msg(1, chat, "hello from polling"), _msg(1, chat, "hello from polling")],  # duplicate delivery
               []]

    def handler(req: httpx.Request) -> httpx.Response:
        method = str(req.url).rsplit("/", 1)[-1]
        body = json.loads(req.content or b"{}")
        calls.append({"method": method, **body})
        if method == "getUpdates":
            if not batches:
                stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": batches.pop(0)})
        return httpx.Response(200, json={"ok": True, "result": True})

    monkeypatch.setattr(telegram_poller, "TRANSPORT", httpx.MockTransport(handler))
    await asyncio.wait_for(telegram_poller.run(stop), timeout=30)

    assert calls[0]["method"] == "deleteWebhook"  # getUpdates can't run while a webhook is set
    polls = [c for c in calls if c["method"] == "getUpdates"]
    assert "offset" not in polls[0] and polls[1]["offset"] == 2  # confirmed only after handling
    async with session_scope() as s:
        link = (await s.execute(select(LinkedChannel).where(LinkedChannel.channel == "telegram",
                                                            LinkedChannel.external_id == str(chat)))).scalar_one()
        tasks = (await s.execute(select(Task).where(Task.user_id == link.user_id))).scalars().all()
    assert [t.goal for t in tasks] == ["hello from polling"]  # the duplicate update made no second task
