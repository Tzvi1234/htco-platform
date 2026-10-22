"""`Idempotency-Key` for unsafe API requests (slice 5.2, AC-5.2.3).

A client that may retry a POST sends an `Idempotency-Key`. The first request with a key stores the
SHA-256 of its method, path and body, runs, and stores its response for 24 hours. A retry with the
same key and the same request gets the stored response and has no second effect; the same key with
another request is refused; a retry while the first is still running is told so. Keys belong to
the signed-in user, so one user's key never replays another user's response. A response of 500 or
more is not stored: the request may be retried for real.
"""

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

KEY_LIFETIME = timedelta(hours=24)
_KEY = re.compile(r"^[\x21-\x7e]{1,255}$")


class InvalidIdempotencyKeyError(ValueError):
    pass


def check_idempotency_key(key: str) -> str:
    if not _KEY.match(key):
        raise InvalidIdempotencyKeyError("an Idempotency-Key is 1 to 255 visible ASCII characters")
    return key


def request_fingerprint(method: str, path: str, body: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in (method.upper().encode("ascii"), path.encode("utf-8"), body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.digest()


@dataclass(frozen=True)
class IdempotentRequest:
    user_id: uuid.UUID
    key: str
    method: str
    path: str
    fingerprint: bytes


@dataclass(frozen=True)
class Started:
    """The first request with this key: run it, then `complete` or `abandon`."""


@dataclass(frozen=True)
class Replay:
    status: int
    body: Mapping[str, Any]


@dataclass(frozen=True)
class KeyReused:
    """The key was used with another request."""


@dataclass(frozen=True)
class StillRunning:
    """The first request with this key has not finished."""


type Begin = Started | Replay | KeyReused | StillRunning


class IdempotencyStore(Protocol):
    async def begin(self, request: IdempotentRequest, *, now: datetime) -> Begin: ...

    async def complete(
        self, request: IdempotentRequest, *, status: int, body: Mapping[str, Any]
    ) -> None: ...

    async def abandon(self, request: IdempotentRequest) -> None: ...


def is_storable(status: int) -> bool:
    """Server errors are not kept: the client may retry the request for real."""
    return status < 500  # noqa: PLR2004 - the class of server errors
