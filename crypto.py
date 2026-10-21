"""Encryption at rest for imgbb API keys and delete links.

Keys and delete links are encrypted with Fernet (AES-128-CBC + HMAC) before
persisting to PostgreSQL, and decrypted only in-memory when needed.
The Fernet secret comes from config (FERNET_KEY) and must be securely stored.

Generate a key once with:
    python generate_key.py
and put the printed value in .env or config.env as FERNET_KEY=...
"""
import logging
import re
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from config import settings

logger = logging.getLogger(__name__)

_fernet_instance: Optional[Fernet] = None
# Standard imgbb API key format is 32 hexadecimal characters
_IMGBB_KEY_PATTERN = re.compile(r"^[a-fA-F0-9]{32}$")


def get_fernet() -> Fernet:
    """Retrieve or initialize the Fernet cipher instance."""
    global _fernet_instance
    if _fernet_instance is None:
        secret = settings.FERNET_KEY
        try:
            key_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
            _fernet_instance = Fernet(key_bytes)
        except Exception as e:
            raise RuntimeError(
                f"Invalid FERNET_KEY configuration: {e}. "
                f"Generate a valid 32-byte url-safe base64 key with `python generate_key.py`."
            ) from e
    return _fernet_instance


def is_valid_imgbb_key(key: Optional[str]) -> bool:
    """Validate if the provided string matches imgbb API key syntax."""
    if not key or not isinstance(key, str):
        return False
    return bool(_IMGBB_KEY_PATTERN.match(key.strip()))


def encrypt(plaintext: str) -> str:
    """Encrypt an API key or delete link for the database. Returns a Fernet token string."""
    cipher = get_fernet()
    return cipher.encrypt(plaintext.strip().encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a stored API key or delete link.

    Backward-compatibility: a raw imgbb key stored before encryption was enabled
    (legacy plaintext) is returned unchanged; it is encrypted next time the user
    saves their key. Any other value that fails decryption raises InvalidToken,
    which usually means FERNET_KEY changed after the key was saved. Returning
    the ciphertext instead would send it to imgbb as if it were a key.
    """
    if not token:
        return ""
    cipher = get_fernet()
    try:
        return cipher.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        if is_valid_imgbb_key(token):
            logger.warning("Stored key is legacy plaintext; it will be encrypted on next save.")
            return token.strip()
        raise
