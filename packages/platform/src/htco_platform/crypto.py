"""Sealing secrets at rest (slice 3.5): AES-256-GCM, each value bound to a context.

The web sessions keep the user's Supabase tokens on the server; this seals them before they reach
the database. The context (the session's ID) is authenticated data, so a sealed token copied into
another session's row does not open. Layout: version byte, 12-byte random nonce, ciphertext+tag.
"""

import base64
import binascii
import os
from typing import override

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = 1
_NONCE_BYTES = 12
_KEY_BYTES = 32
_TAG_BYTES = 16


class SealingError(Exception):
    """The value could not be opened: wrong key or context, tampered, or malformed."""


class Sealer:
    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise ValueError("the sealing key must be a 32-byte key")
        self._aead = AESGCM(key)

    @classmethod
    def from_base64(cls, key: str) -> Sealer:
        try:
            raw = base64.b64decode(key, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("the sealing key must be a 32-byte key, base64-encoded") from error
        return cls(raw)

    def seal(self, plaintext: str, *, context: bytes) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext.encode("utf-8"), _associated(context))
        return bytes([_VERSION]) + nonce + sealed

    def open(self, blob: bytes, *, context: bytes) -> str:
        if len(blob) < 1 + _NONCE_BYTES + _TAG_BYTES or blob[0] != _VERSION:
            raise SealingError("not a sealed value")
        nonce, sealed = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
        try:
            return self._aead.decrypt(nonce, sealed, _associated(context)).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as error:
            raise SealingError("the sealed value does not open") from error

    @override
    def __repr__(self) -> str:
        return "Sealer(<key hidden>)"


def _associated(context: bytes) -> bytes:
    return bytes([_VERSION]) + context
