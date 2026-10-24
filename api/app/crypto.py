"""Fernet encryption for secrets stored as *_encrypted bytea columns (ingest secret, Slack URL).

The ingest secret must be readable to verify HMAC signatures, so it is encrypted, not hashed.
Decrypted values are never returned by an endpoint (except the one-time display on create/rotate).
"""

from functools import lru_cache

from cryptography.fernet import Fernet

from .config import get_settings


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().APP_ENCRYPTION_KEY)


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(bytes(token)).decode()
