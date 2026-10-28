"""An in-memory `IdempotencyStore` with the Postgres store's rules (slice 5.2)."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from htco_application.idempotency import (
    KEY_LIFETIME,
    Begin,
    IdempotentRequest,
    KeyReused,
    Replay,
    Started,
    StillRunning,
)


async def fresh_idempotency_key(request: httpx.Request) -> None:
    """An httpx request hook: a new `Idempotency-Key` on every request that names none, for
    tests of routes that require one (architecture audit 6.1) but are not about replays."""
    if "Idempotency-Key" not in request.headers:
        request.headers["Idempotency-Key"] = str(uuid.uuid4())


@dataclass
class Stored:
    fingerprint: bytes
    expires_at: datetime
    status: int | None = None
    body: Mapping[str, Any] | None = None


@dataclass
class MemoryIdempotencyStore:
    rows: dict[tuple[uuid.UUID, str], Stored] = field(default_factory=dict)

    async def begin(self, request: IdempotentRequest, *, now: datetime) -> Begin:
        slot = (request.user_id, request.key)
        held = self.rows.get(slot)
        if held is not None and held.expires_at <= now:
            del self.rows[slot]
            held = None
        if held is None:
            self.rows[slot] = Stored(request.fingerprint, now + KEY_LIFETIME)
            return Started()
        if held.fingerprint != request.fingerprint:
            return KeyReused()
        if held.status is None or held.body is None:
            return StillRunning()
        return Replay(held.status, held.body)

    async def complete(
        self, request: IdempotentRequest, *, status: int, body: Mapping[str, Any]
    ) -> None:
        held = self.rows[(request.user_id, request.key)]
        held.status, held.body = status, dict(body)

    async def abandon(self, request: IdempotentRequest) -> None:
        self.rows.pop((request.user_id, request.key), None)
