"""The profile lock's bookkeeping (slice 8A.2), against a fake advisory-lock server.

Postgres keeps an advisory lock per connection; `PostgresProfileLocks` remembers which of its
connections holds which environment, so `still_held` can ask on the right one. The real
database is in `tests/integration/test_browser_session_store.py`.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from htco_persistence.browser_sessions import PostgresProfileLocks
from htco_persistence.database import Database

pytestmark = pytest.mark.anyio


@dataclass
class _Row:
    value: bool

    def fetchone(self) -> tuple[bool]:
        return (self.value,)


@dataclass
class _Server:
    """Advisory locks by key, each owned by one connection until it unlocks or closes."""

    owners: dict[str, FakeConnection] = field(default_factory=dict)


@dataclass(eq=False)
class FakeConnection:
    server: _Server

    def execute(self, sql: str, params: Sequence[Any]) -> _Row:
        key = str(params[0])
        if "pg_try_advisory_lock" in sql:
            return _Row(self.server.owners.setdefault(key, self) is self)
        if "pg_advisory_unlock" in sql:
            if self.server.owners.get(key) is self:
                del self.server.owners[key]
            return _Row(True)
        assert "pg_locks" in sql
        return _Row(self.server.owners.get(key) is self)

    def close(self) -> None:  # a closed connection's locks are released
        for key in [k for k, owner in self.server.owners.items() if owner is self]:
            del self.server.owners[key]


@dataclass
class FakeDatabase:
    server: _Server = field(default_factory=_Server)

    def dedicated_connection(self) -> FakeConnection:
        return FakeConnection(self.server)


def soon(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def test_still_held__a_deferred_waiter_leaves_the_holder_tracked() -> None:
    """Codex on #121: a second session that gives up waiting must not erase the first one's
    entry, or the first would wrongly see its lock as lost and discard its profile."""
    locks = PostgresProfileLocks(cast("Database", FakeDatabase()), poll_seconds=0.01)
    environment = uuid.uuid4()
    async with locks.hold(environment, until=soon(1)) as first:
        assert first
        async with locks.hold(environment, until=soon(0.05)) as second:
            assert not second
        assert await locks.still_held(environment)
    assert not await locks.still_held(environment)


async def test_still_held__false_for_an_environment_this_process_never_locked() -> None:
    locks = PostgresProfileLocks(cast("Database", FakeDatabase()), poll_seconds=0.01)
    assert not await locks.still_held(uuid.uuid4())
