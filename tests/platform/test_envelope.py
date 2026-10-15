"""Envelope encryption for the token vault (slice 4.2): a data key per record, wrapped by a ring.

Each record gets a fresh AES-256-GCM data key; the data key is wrapped by the ring's current key
and stored next to its key ID, so rotating the ring only re-wraps the data key. The context (the
record's identity) is authenticated data, so neither a wrapped key nor a ciphertext opens in
another record.
"""

import base64
import os
from dataclasses import replace

import pytest

from htco_platform.crypto import Envelope, EnvelopeSealer, KeyRing, SealingError

KEY_A = os.urandom(32)
KEY_B = os.urandom(32)
TOKEN = "etsy-access-token-0123456789"  # noqa: S105 - a test value
REFRESH = "etsy-refresh-token-9876543210"


def entry(key_id: str, key: bytes) -> str:
    return f"{key_id}:{base64.b64encode(key).decode()}"


def ring_a() -> KeyRing:
    return KeyRing(("key-a", KEY_A))


def ring_b_after_a() -> KeyRing:
    return KeyRing(("key-b", KEY_B), [("key-a", KEY_A)])


def test_seal_then_open__returns_every_plaintext_in_order() -> None:
    sealer = EnvelopeSealer(ring_a())

    envelope = sealer.seal([TOKEN, REFRESH], context=b"shop-1")

    assert envelope.key_id == "key-a"
    assert sealer.open(envelope, context=b"shop-1") == (TOKEN, REFRESH)


def test_seal__no_plaintext_in_any_stored_part_and_a_fresh_data_key_each_time() -> None:
    sealer = EnvelopeSealer(ring_a())

    first = sealer.seal([TOKEN, REFRESH], context=b"shop-1")
    second = sealer.seal([TOKEN, REFRESH], context=b"shop-1")

    for part in (first.wrapped_key, *first.ciphertexts):
        assert TOKEN.encode() not in part
        assert REFRESH.encode() not in part
        assert KEY_A not in part
    assert first.wrapped_key != second.wrapped_key
    assert first.ciphertexts != second.ciphertexts


def test_open__in_another_context__fails() -> None:
    """A row's ciphertext copied into another shop's row does not open there."""
    sealer = EnvelopeSealer(ring_a())
    envelope = sealer.seal([TOKEN], context=b"shop-1")

    with pytest.raises(SealingError):
        sealer.open(envelope, context=b"shop-2")


def test_open__ciphertexts_swapped_between_records__fail() -> None:
    sealer = EnvelopeSealer(ring_a())
    one = sealer.seal([TOKEN], context=b"shop-1")
    two = sealer.seal([REFRESH], context=b"shop-1")

    with pytest.raises(SealingError):
        sealer.open(replace(one, ciphertexts=two.ciphertexts), context=b"shop-1")


def test_open__access_and_refresh_ciphertexts_swapped__fail() -> None:
    sealer = EnvelopeSealer(ring_a())
    envelope = sealer.seal([TOKEN, REFRESH], context=b"shop-1")
    access, refresh = envelope.ciphertexts

    with pytest.raises(SealingError):
        sealer.open(replace(envelope, ciphertexts=(refresh, access)), context=b"shop-1")


def test_open__relabelled_key_id__fails() -> None:
    sealer = EnvelopeSealer(KeyRing(("key-b", KEY_B), [("key-a", KEY_B)]))
    envelope = sealer.seal([TOKEN], context=b"shop-1")

    with pytest.raises(SealingError):
        sealer.open(replace(envelope, key_id="key-a"), context=b"shop-1")


@pytest.mark.parametrize("part", ["wrapped_key", "ciphertext"])
def test_open__tampered__fails(part: str) -> None:
    sealer = EnvelopeSealer(ring_a())
    envelope = sealer.seal([TOKEN], context=b"shop-1")
    if part == "wrapped_key":
        flipped = bytearray(envelope.wrapped_key)
        flipped[-1] ^= 0x01
        envelope = replace(envelope, wrapped_key=bytes(flipped))
    else:
        flipped = bytearray(envelope.ciphertexts[0])
        flipped[-1] ^= 0x01
        envelope = replace(envelope, ciphertexts=(bytes(flipped),))

    with pytest.raises(SealingError):
        sealer.open(envelope, context=b"shop-1")


@pytest.mark.parametrize("blob", [b"", b"\x01", b"\x02" + b"x" * 40, b"\x01short"])
def test_open__malformed_parts__fail(blob: bytes) -> None:
    sealer = EnvelopeSealer(ring_a())
    envelope = sealer.seal([TOKEN], context=b"c")

    with pytest.raises(SealingError):
        sealer.open(replace(envelope, wrapped_key=blob), context=b"c")
    with pytest.raises(SealingError):
        sealer.open(replace(envelope, ciphertexts=(blob,)), context=b"c")


def test_open__unknown_key_id__fails_naming_only_the_id() -> None:
    old = EnvelopeSealer(KeyRing(("key-old", os.urandom(32))))
    envelope = old.seal([TOKEN], context=b"c")

    with pytest.raises(SealingError, match="key-old") as raised:
        EnvelopeSealer(ring_a()).open(envelope, context=b"c")
    assert TOKEN not in str(raised.value)


@pytest.mark.ac("AC-4.2.2")
def test_previous_key__still_opens_and_rewrap_moves_to_the_current_key() -> None:
    written = EnvelopeSealer(ring_a()).seal([TOKEN, REFRESH], context=b"shop-1")
    rotating = EnvelopeSealer(ring_b_after_a())

    assert rotating.open(written, context=b"shop-1") == (TOKEN, REFRESH)
    rewrapped = rotating.rewrap(written, context=b"shop-1")

    assert rewrapped.key_id == "key-b"
    assert rewrapped.ciphertexts == written.ciphertexts  # only the data key is re-encrypted
    assert rotating.open(rewrapped, context=b"shop-1") == (TOKEN, REFRESH)
    with pytest.raises(SealingError):
        EnvelopeSealer(ring_a()).open(rewrapped, context=b"shop-1")  # key A alone no longer opens


def test_rewrap__in_another_context__fails() -> None:
    written = EnvelopeSealer(ring_a()).seal([TOKEN], context=b"shop-1")

    with pytest.raises(SealingError):
        EnvelopeSealer(ring_b_after_a()).rewrap(written, context=b"shop-2")


def test_seal__always_uses_the_current_key() -> None:
    envelope = EnvelopeSealer(ring_b_after_a()).seal([TOKEN], context=b"c")
    assert envelope.key_id == "key-b"


# The key ring -----------------------------------------------------------------------------------


def test_parse__current_and_previous_entries() -> None:
    ring = KeyRing.parse(entry("key-b", KEY_B), f" {entry('key-a', KEY_A)} , ")

    assert ring.current_key_id == "key-b"
    assert ring.key_ids == ("key-b", "key-a")


def test_parse__no_previous_keys() -> None:
    assert KeyRing.parse(entry("key-a", KEY_A)).key_ids == ("key-a",)
    assert KeyRing.parse(entry("key-a", KEY_A), "").key_ids == ("key-a",)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no-separator",
        ":" + base64.b64encode(KEY_A).decode(),
        "bad id!:" + base64.b64encode(KEY_A).decode(),
        "k" * 33 + ":" + base64.b64encode(KEY_A).decode(),
        "key-a:not base64!",
        "key-a:" + base64.b64encode(b"short").decode(),
    ],
    ids=["empty", "no-id", "blank-id", "bad-id", "long-id", "not-base64", "short-key"],
)
def test_parse__invalid_entry__is_refused_without_repeating_it(value: str) -> None:
    with pytest.raises(ValueError, match="key ID") as raised:
        KeyRing.parse(value)
    secret = value.partition(":")[2]
    if secret:
        assert secret not in str(raised.value)


def test_parse__duplicate_key_ids__are_refused() -> None:
    with pytest.raises(ValueError, match="key-a"):
        KeyRing.parse(entry("key-a", KEY_A), entry("key-a", KEY_B))


def test_key_ring__needs_32_byte_keys() -> None:
    with pytest.raises(ValueError, match="32-byte"):
        KeyRing(("key-a", b"short"))


@pytest.mark.ac("AC-4.2.3")
def test_reprs__show_key_ids_but_never_keys_or_plaintext() -> None:
    ring = KeyRing.parse(entry("key-b", KEY_B), entry("key-a", KEY_A))
    sealer = EnvelopeSealer(ring)
    envelope = sealer.seal([TOKEN, REFRESH], context=b"c")

    shown = repr(ring) + repr(sealer) + repr(envelope) + str(envelope)

    assert "key-b" in repr(ring)
    for secret in (base64.b64encode(KEY_A).decode(), base64.b64encode(KEY_B).decode()):
        assert secret not in shown
    assert TOKEN not in shown
    assert REFRESH not in shown
    assert repr(envelope.wrapped_key) not in shown


def test_envelope__is_a_plain_value() -> None:
    envelope = Envelope(key_id="k", wrapped_key=b"w", ciphertexts=(b"c",))
    assert envelope == Envelope(key_id="k", wrapped_key=b"w", ciphertexts=(b"c",))
