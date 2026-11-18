"""AES-256-GCM encryption for provider tokens and short-lived secrets (OTPs).

The key comes only from APNA_ENCRYPTION_KEY (32 random bytes, base64url). There is no built-in default:
without a key, linking accounts and OTP entry are refused rather than storing secrets in plain text.
Every ciphertext is bound to its owner via associated data (e.g. "<user_id>:<provider>"), so a
ciphertext copied to another user's or provider's row fails to decrypt.

Generate a key:  python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
"""

from __future__ import annotations

import base64
import os

from apna.core.errors import ApnaError

_VERSION = "v1"


class EncryptionUnavailable(ApnaError):
    code = "encryption_unavailable"
    http_status = 503


def _key() -> bytes:
    from apna.core.config import get_settings

    raw = get_settings().apna_encryption_key
    if raw is None or not raw.get_secret_value():
        raise EncryptionUnavailable("APNA_ENCRYPTION_KEY is not configured; secrets can't be stored.")
    key = base64.urlsafe_b64decode(raw.get_secret_value() + "=" * (-len(raw.get_secret_value()) % 4))
    if len(key) != 32:
        raise EncryptionUnavailable("APNA_ENCRYPTION_KEY must be 32 bytes (base64url).")
    return key


def encrypt(plaintext: str, aad: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode(), aad.encode())
    return f"{_VERSION}:" + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(token: str, aad: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    version, _, body = token.partition(":")
    if version != _VERSION:
        raise ValueError("unknown ciphertext version")
    blob = base64.urlsafe_b64decode(body)
    try:
        return AESGCM(_key()).decrypt(blob[:12], blob[12:], aad.encode()).decode()
    except InvalidTag as exc:
        raise ValueError("ciphertext does not belong to this owner or was tampered with") from exc
