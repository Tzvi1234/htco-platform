"""Sealing secrets at rest (slice 3.5): AES-256-GCM bound to a context."""

import base64
import os

import pytest

from htco_platform.crypto import Sealer, SealingError

KEY = base64.b64encode(os.urandom(32)).decode()


def test_seal_then_open__returns_the_plaintext() -> None:
    sealer = Sealer.from_base64(KEY)
    sealed = sealer.seal("access-token-value", context=b"session-1")
    assert sealer.open(sealed, context=b"session-1") == "access-token-value"


def test_seal__never_contains_the_plaintext_and_differs_each_time() -> None:
    sealer = Sealer.from_base64(KEY)
    first = sealer.seal("same secret", context=b"c")
    second = sealer.seal("same secret", context=b"c")
    assert b"same secret" not in first
    assert first != second  # a fresh nonce every time


def test_open__with_another_context__fails() -> None:
    """A token sealed for one session cannot be moved to another session's row."""
    sealer = Sealer.from_base64(KEY)
    sealed = sealer.seal("token", context=b"session-1")
    with pytest.raises(SealingError):
        sealer.open(sealed, context=b"session-2")


def test_open__tampered_or_with_another_key__fails() -> None:
    sealer = Sealer.from_base64(KEY)
    sealed = bytearray(sealer.seal("token", context=b"c"))
    sealed[-1] ^= 0x01
    with pytest.raises(SealingError):
        sealer.open(bytes(sealed), context=b"c")
    other = Sealer.from_base64(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(SealingError):
        other.open(sealer.seal("token", context=b"c"), context=b"c")


@pytest.mark.parametrize("blob", [b"", b"\x01", b"\x02" + b"x" * 40, b"\x01short"])
def test_open__malformed_blob__fails(blob: bytes) -> None:
    with pytest.raises(SealingError):
        Sealer.from_base64(KEY).open(blob, context=b"c")


@pytest.mark.parametrize("key", ["", "not base64!", base64.b64encode(b"short").decode()])
def test_from_base64__needs_a_32_byte_key(key: str) -> None:
    with pytest.raises(ValueError, match="32-byte"):
        Sealer.from_base64(key)


def test_repr__hides_the_key() -> None:
    assert KEY not in repr(Sealer.from_base64(KEY))
