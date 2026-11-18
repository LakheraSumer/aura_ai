from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apna.core.errors import Unauthorized
from apna.db.models import User
from apna.db.session import get_db
from apna.security.auth import decode_access_token
from apna.services.conversation import ConversationService

# scope="function": commit runs before the response is sent. With the default scope the commit runs
# after the response, so a client's very next request can miss the row it was just told exists.
DB = Annotated[AsyncSession, Depends(get_db, scope="function")]


async def current_user(db: DB, authorization: Annotated[str | None, Header()] = None) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token")
    user_id = decode_access_token(authorization.split(" ", 1)[1])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active or user.deleted_at is not None:
        raise Unauthorized("Account not active")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def conversation_service(request: Request) -> ConversationService:
    return request.app.state.conversations


Conversations = Annotated[ConversationService, Depends(conversation_service)]
