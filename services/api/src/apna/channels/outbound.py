"""Deliver assistant messages to every linked, opted-in channel and record the provider outcome."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.channels.base import ChannelAdapter
from apna.channels.telegram import TelegramAdapter
from apna.channels.whatsapp import WhatsAppAdapter
from apna.core.config import get_settings
from apna.db.models import LinkedChannel, Message, OutboundMessage

# Overridable in tests to inject an httpx.MockTransport.
ADAPTER_FACTORIES: dict[str, Callable[[], ChannelAdapter]] = {
    "whatsapp": lambda: WhatsAppAdapter(get_settings()),
    "telegram": lambda: TelegramAdapter(get_settings()),
}


async def deliver_to_user(session: AsyncSession, user_id: uuid.UUID, msg: Message, *,
                          proactive: bool = False) -> list[OutboundMessage]:
    links = (await session.execute(
        select(LinkedChannel).where(LinkedChannel.user_id == user_id, LinkedChannel.opted_in.is_(True))
    )).scalars().all()
    out: list[OutboundMessage] = []
    actions = (msg.payload or {}).get("actions", [])
    for link in links:
        dedupe = f"{msg.id}:{link.channel}:{link.external_id}"
        if (await session.execute(select(OutboundMessage.id).where(OutboundMessage.dedupe_key == dedupe))).first():
            continue
        factory = ADAPTER_FACTORIES.get(link.channel)
        if factory is None:
            continue
        outcome = await factory().send(link.external_id, msg.content, actions,
                                       last_inbound_at=link.last_inbound_at, proactive=proactive)
        row = OutboundMessage(user_id=user_id, channel=link.channel, external_id=link.external_id,
                              message_id=msg.id, kind=outcome.kind, text=msg.content, payload={"actions": actions},
                              status=outcome.status, provider_message_id=outcome.provider_message_id,
                              error=outcome.error, dedupe_key=dedupe)
        session.add(row)
        out.append(row)
    await session.flush()
    return out
