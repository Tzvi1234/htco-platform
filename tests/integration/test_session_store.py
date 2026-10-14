"""The web session store against real Postgres (slice 3.5)."""

import base64
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.web_sessions import IssuedTokens, NewSession
from htco_persistence.database import Database
from htco_persistence.web_sessions import PostgresSessionStore
from htco_platform.crypto import Sealer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)
SEALER = Sealer.from_base64(base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    url = os.environ.get("HTCO_TEST_DATABASE_URL")
    if not url:
        pytest.fail("HTCO_TEST_DATABASE_URL is not set; start the database with `just db-start`")
    db = Database(url, min_size=1, max_size=2)
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


def new_user(database: Database) -> uuid.UUID:
    user_id = uuid.uuid4()
    with database.connection() as connection:
        connection.execute(
            "insert into auth.users (id, email) values (%s, %s)",
            [user_id, f"session-{user_id.hex[:12]}@example.test"],
        )
    return user_id


def session(user_id: uuid.UUID) -> NewSession:
    return NewSession(
        id=uuid.uuid4(),
        token_hash=hashlib.sha256(uuid.uuid4().bytes).digest(),
        user_id=user_id,
        tokens=IssuedTokens("access-token-1", "refresh-token-1", NOW + timedelta(hours=1)),
        created_at=NOW,
        idle_expires_at=NOW + timedelta(hours=12),
        absolute_expires_at=NOW + timedelta(days=14),
        user_agent="Firefox",
    )


async def test_create_find__round_trips_with_tokens_sealed_at_rest(database: Database) -> None:
    store = PostgresSessionStore(database, SEALER)
    created = session(new_user(database))
    await store.create(created)

    found = await store.find(created.token_hash)

    assert found is not None
    assert found.tokens == created.tokens
    assert (found.user_id, found.user_agent, found.revoked_at) == (created.user_id, "Firefox", None)
    with database.connection() as connection:
        row = connection.execute(
            "select access_token, refresh_token from private.web_sessions where id = %s",
            [created.id],
        ).fetchone()
    assert row is not None
    assert b"access-token-1" not in bytes(row[0])
    assert b"refresh-token-1" not in bytes(row[1])


async def test_replace_touch_revoke(database: Database) -> None:
    store = PostgresSessionStore(database, SEALER)
    created = session(new_user(database))
    await store.create(created)

    await store.replace_tokens(created.id, IssuedTokens("a2", "r2", NOW + timedelta(hours=2)))
    await store.touch(created.id, NOW + timedelta(minutes=5), NOW + timedelta(hours=13))
    await store.revoke(created.id, NOW + timedelta(minutes=6))
    await store.revoke(created.id, NOW + timedelta(minutes=9))  # already revoked: unchanged

    found = await store.find(created.token_hash)
    assert found is not None
    assert (found.tokens.access_token, found.tokens.refresh_token) == ("a2", "r2")
    assert found.last_seen_at == NOW + timedelta(minutes=5)
    assert found.idle_expires_at == NOW + timedelta(hours=13)
    assert found.revoked_at == NOW + timedelta(minutes=6)


async def test_unknown_hash__finds_nothing(database: Database) -> None:
    assert await PostgresSessionStore(database, SEALER).find(b"\x00" * 32) is None


async def test_attempts__counted_per_email_and_per_address_within_the_window(
    database: Database,
) -> None:
    store = PostgresSessionStore(database, SEALER)
    email, other_email = os.urandom(32), os.urandom(32)
    ip = os.urandom(32)
    await store.record_attempt(email, ip, succeeded=False, at=NOW)
    await store.record_attempt(email, None, succeeded=False, at=NOW)
    await store.record_attempt(other_email, ip, succeeded=False, at=NOW)
    await store.record_attempt(email, ip, succeeded=True, at=NOW)
    await store.record_attempt(email, ip, succeeded=False, at=NOW - timedelta(hours=1))

    assert await store.recent_failures(email, ip, NOW - timedelta(minutes=15)) == (2, 2)
    assert await store.recent_failures(email, None, NOW - timedelta(minutes=15)) == (2, 0)
