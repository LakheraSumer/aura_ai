"""Password hashing (Argon2id) and JWT access / opaque refresh tokens."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from apna.core.config import get_settings
from apna.core.errors import Unauthorized

_hasher = PasswordHasher()
_ALG = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=s.jwt_access_ttl_seconds)).timestamp()),
        "typ": "access",
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=_ALG), s.jwt_access_ttl_seconds


def decode_access_token(token: str) -> uuid.UUID:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret.get_secret_value(), algorithms=[_ALG])
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid or expired access token") from exc
    if payload.get("typ") != "access":
        raise Unauthorized("Wrong token type")
    return uuid.UUID(payload["sub"])


def new_refresh_token() -> tuple[str, str]:
    """Returns (plaintext token for the client, sha256 hash for storage)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
