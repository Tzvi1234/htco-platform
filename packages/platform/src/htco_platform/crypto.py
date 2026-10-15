"""Sealing secrets at rest: AES-256-GCM, each value bound to a context.

`Sealer` (slice 3.5) seals with one key. The web sessions keep the user's Supabase tokens on the
server; this seals them before they reach the database. The context (the session's ID) is
authenticated data, so a sealed token copied into another session's row does not open. Layout:
version byte, 12-byte random nonce, ciphertext+tag.

`EnvelopeSealer` (slice 4.2) seals each record with its own data key, wrapped by a `KeyRing` that
supports rotation (see below).
"""

import base64
import binascii
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
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


# Envelope encryption (slice 4.2) -----------------------------------------------------------------
#
# Each record gets a fresh 32-byte data key. Its values are sealed with that data key, and the data
# key is sealed ("wrapped") with the ring's current key-encryption key, whose ID is stored next to
# it. Rotation re-wraps only the data key, so it is cheap and never touches the values. The
# record's context (its identity) is authenticated data for both layers, the value's position for
# the values and the key ID for the wrapped key, so no part opens in another record, another
# position or under another key ID. Both layers use the layout above.

_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_KEY_FORMAT = (
    "each key must be <key ID>:<32-byte key, base64>, with a key ID of 1 to 32 letters, digits, "
    "'.', '_' or '-'"
)


class KeyRing:
    """The key-encryption keys: the current one wraps new data keys, the previous ones only open."""

    def __init__(
        self, current: tuple[str, bytes], previous: Sequence[tuple[str, bytes]] = ()
    ) -> None:
        keys: dict[str, AESGCM] = {}
        for key_id, key in (current, *previous):
            if not _KEY_ID.match(key_id):
                raise ValueError(_KEY_FORMAT)
            if len(key) != _KEY_BYTES:
                raise ValueError("a key-encryption key must be a 32-byte key")
            if key_id in keys:
                raise ValueError(f"the key ID {key_id!r} is used twice")
            keys[key_id] = AESGCM(key)
        self._keys = keys
        self._current = current[0]

    @classmethod
    def parse(cls, current: str, previous: str | None = None) -> KeyRing:
        """Read `<id>:<base64>` entries: the current key, and previous keys separated by commas.

        Errors describe the expected format and never repeat a key.
        """
        entries = [entry for entry in (previous or "").split(",") if entry.strip()]
        return cls(_parse_entry(current), [_parse_entry(entry) for entry in entries])

    @property
    def current_key_id(self) -> str:
        return self._current

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    @property
    def previous_key_ids(self) -> tuple[str, ...]:
        return tuple(key_id for key_id in self._keys if key_id != self._current)

    def aead(self, key_id: str) -> AESGCM:
        try:
            return self._keys[key_id]
        except KeyError:
            raise SealingError(f"no key with ID {key_id!r} is configured") from None

    @override
    def __repr__(self) -> str:
        return f"KeyRing(current={self._current!r}, previous={list(self.previous_key_ids)!r})"


def _parse_entry(entry: str) -> tuple[str, bytes]:
    key_id, separator, encoded = entry.strip().partition(":")
    if not separator:
        raise ValueError(_KEY_FORMAT)
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except binascii.Error, ValueError:
        raise ValueError(_KEY_FORMAT) from None
    if not _KEY_ID.match(key_id) or len(key) != _KEY_BYTES:
        raise ValueError(_KEY_FORMAT)
    return key_id, key


@dataclass(frozen=True)
class Envelope:
    """What is stored for one record: the key ID, the wrapped data key and the sealed values."""

    key_id: str
    wrapped_key: bytes = field(repr=False)
    ciphertexts: tuple[bytes, ...] = field(repr=False)


class EnvelopeSealer:
    def __init__(self, keys: KeyRing) -> None:
        self._keys = keys

    @property
    def current_key_id(self) -> str:
        return self._keys.current_key_id

    @property
    def previous_key_ids(self) -> tuple[str, ...]:
        return self._keys.previous_key_ids

    def seal(self, plaintexts: Sequence[str], *, context: bytes) -> Envelope:
        data_key = AESGCM.generate_key(bit_length=256)
        aead = AESGCM(data_key)
        ciphertexts = tuple(
            _seal(aead, plaintext.encode("utf-8"), _data_associated(context, index))
            for index, plaintext in enumerate(plaintexts)
        )
        return Envelope(self.current_key_id, self._wrap(data_key, context), ciphertexts)

    def open(self, envelope: Envelope, *, context: bytes) -> tuple[str, ...]:
        aead = AESGCM(self._unwrap(envelope, context))
        opened = (
            _open(aead, blob, _data_associated(context, index))
            for index, blob in enumerate(envelope.ciphertexts)
        )
        try:
            return tuple(value.decode("utf-8") for value in opened)
        except UnicodeDecodeError as error:  # pragma: no cover - only we seal, always UTF-8
            raise SealingError("the sealed value does not open") from error

    def rewrap(self, envelope: Envelope, *, context: bytes) -> Envelope:
        """The same sealed values, with their data key wrapped by the current key."""
        data_key = self._unwrap(envelope, context)
        return Envelope(self.current_key_id, self._wrap(data_key, context), envelope.ciphertexts)

    def _wrap(self, data_key: bytes, context: bytes) -> bytes:
        key_id = self.current_key_id
        return _seal(self._keys.aead(key_id), data_key, _key_associated(key_id, context))

    def _unwrap(self, envelope: Envelope, context: bytes) -> bytes:
        aead = self._keys.aead(envelope.key_id)
        return _open(aead, envelope.wrapped_key, _key_associated(envelope.key_id, context))

    @override
    def __repr__(self) -> str:
        return f"EnvelopeSealer({self._keys!r})"


def _seal(aead: AESGCM, plaintext: bytes, associated: bytes) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return bytes([_VERSION]) + nonce + aead.encrypt(nonce, plaintext, associated)


def _open(aead: AESGCM, blob: bytes, associated: bytes) -> bytes:
    if len(blob) < 1 + _NONCE_BYTES + _TAG_BYTES or blob[0] != _VERSION:
        raise SealingError("not a sealed value")
    nonce, sealed = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
    try:
        return aead.decrypt(nonce, sealed, associated)
    except InvalidTag as error:
        raise SealingError("the sealed value does not open") from error


def _framed(*parts: bytes) -> bytes:
    """Length-prefixed parts, so two different lists of parts never give the same bytes."""
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def _key_associated(key_id: str, context: bytes) -> bytes:
    return _framed(b"htco-envelope-key", bytes([_VERSION]), key_id.encode("ascii"), context)


def _data_associated(context: bytes, index: int) -> bytes:
    return _framed(b"htco-envelope-data", bytes([_VERSION]), context, index.to_bytes(2, "big"))
