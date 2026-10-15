"""The web session store against real Postgres (slice 3.5)."""

import base64
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from htco_application.web_sessions import (
    MAX_FAILURES_PER_EMAIL,
    AttemptOutcome,
    ClientInfo,
    IssuedTokens,
    NewSession,
    sign_in,
)
from htco_persistence.database import Database
from htco_persistence.web_sessions import PostgresSessionStore
from htco_platform.crypto import Sealer
from tests.session_fakes import Auth, Tokens, at_once

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


async def test_claim_link__once_until_released(database: Database) -> None:
    store = PostgresSessionStore(database, SEALER)
    link, old = os.urandom(32), os.urandom(32)
    since = NOW - timedelta(days=1)
    await store.record_attempt(old, None, succeeded=True, at=NOW - timedelta(days=2))

    assert await store.claim_link(link, since, NOW) is True
    assert await store.claim_link(link, since, NOW) is False
    await store.release_link(link)
    assert await store.claim_link(link, since, NOW) is True
    assert await store.claim_link(old, since, NOW) is True  # an old success is past the window
    assert await store.recent_failures(link, None, since) == (0, 0)  # claims are not failures


async def test_claim_link__concurrent_claims__one_wins(database: Database) -> None:
    store = PostgresSessionStore(database, SEALER)
    link = os.urandom(32)
    won: list[bool] = []

    async def claim() -> None:
        won.append(await store.claim_link(link, NOW - timedelta(days=1), NOW))

    async with anyio.create_task_group() as group:
        for _ in range(5):
            group.start_soon(claim)

    assert sorted(won) == [False, False, False, False, True]


async def test_admit_attempt__concurrent_burst__admits_only_the_limit(database: Database) -> None:
    store = PostgresSessionStore(database, SEALER)
    key, ip = os.urandom(32), os.urandom(32)
    since = NOW - timedelta(minutes=15)
    admitted: list[bool] = []

    async def admit() -> None:
        admitted.append(
            await store.admit_attempt(key, ip, max_by_key=3, max_by_ip=20, since=since, at=NOW)
        )

    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(admit)

    assert admitted.count(True) == 3
    assert await store.recent_failures(key, ip, since) == (3, 3)


async def test_reserve_attempt__concurrent_burst__reserves_only_the_limit(
    database: Database,
) -> None:
    store = PostgresSessionStore(database, SEALER)
    key, ip = os.urandom(32), os.urandom(32)
    since = NOW - timedelta(minutes=15)
    reserved: list[int | None] = []

    async def reserve() -> None:
        reserved.append(
            await store.reserve_attempt(key, ip, max_by_key=3, max_by_ip=20, since=since, at=NOW)
        )

    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(reserve)

    ids = [attempt for attempt in reserved if attempt is not None]
    assert len(set(ids)) == 3
    assert await store.recent_failures(key, ip, since) == (3, 3)  # in flight, they count


async def test_reserve_attempt__concurrent_burst_from_one_address__reserves_only_its_limit(
    database: Database,
) -> None:
    store = PostgresSessionStore(database, SEALER)
    ip = os.urandom(32)
    since = NOW - timedelta(minutes=15)
    reserved: list[int | None] = []

    async def reserve() -> None:
        reserved.append(
            await store.reserve_attempt(
                os.urandom(32), ip, max_by_key=5, max_by_ip=3, since=since, at=NOW
            )
        )

    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(reserve)

    assert len([attempt for attempt in reserved if attempt is not None]) == 3


async def test_settle_attempt__failed_is_kept_the_others_free_their_place(
    database: Database,
) -> None:
    store = PostgresSessionStore(database, SEALER)
    key, ip = os.urandom(32), os.urandom(32)
    since = NOW - timedelta(minutes=15)

    async def reserve() -> int | None:
        return await store.reserve_attempt(key, ip, max_by_key=3, max_by_ip=20, since=since, at=NOW)

    failed, succeeded, dropped = await reserve(), await reserve(), await reserve()
    assert None not in (failed, succeeded, dropped)
    assert await reserve() is None
    await store.settle_attempt(failed, AttemptOutcome.FAILED)  # type: ignore[arg-type]
    await store.settle_attempt(succeeded, AttemptOutcome.SUCCEEDED)  # type: ignore[arg-type]
    await store.settle_attempt(dropped, AttemptOutcome.NOT_COUNTED)  # type: ignore[arg-type]

    assert await store.recent_failures(key, ip, since) == (1, 1)
    with database.connection() as connection:
        rows = connection.execute(
            "select succeeded from private.sign_in_attempts where email_hash = %s order by id",
            [key],
        ).fetchall()
    assert [row[0] for row in rows] == [False, True]
    assert await reserve() is not None


async def test_sign_in__concurrent_wrong_passwords__only_the_limit_reaches_supabase(
    database: Database,
) -> None:
    """The burst the check-then-count throttle let through, against the real attempt log."""
    store = PostgresSessionStore(database, SEALER)
    email = f"burst-{uuid.uuid4().hex[:12]}@example.test"
    run = uuid.uuid4().hex[:4]

    async def wrong(n: int) -> None:
        await sign_in(
            email,
            "x",
            client=ClientInfo(f"2001:db8::{run}:{n}", None),
            auth=Auth(),
            tokens=Tokens(),
            store=store,
            now=NOW,
        )

    outcomes = await at_once(MAX_FAILURES_PER_EMAIL * 3, wrong)

    assert outcomes.count("SignInRefusedError") == MAX_FAILURES_PER_EMAIL
    assert outcomes.count("TooManyAttemptsError") == MAX_FAILURES_PER_EMAIL * 2


async def test_step_up_get_and_list_active(database: Database) -> None:
    store = PostgresSessionStore(database, SEALER)
    user = new_user(database)
    first, second, third = session(user), session(user), session(user)
    for created in (first, second, third):
        await store.create(created)
    await store.revoke(third.id, NOW)
    await store.set_reauthenticated(first.id, NOW + timedelta(minutes=1))

    got = await store.get(first.id)
    active = await store.list_active(user, NOW + timedelta(minutes=2))

    assert got is not None
    assert got.reauthenticated_at == NOW + timedelta(minutes=1)
    assert {s.id for s in active} == {first.id, second.id}
    assert await store.get(uuid.uuid4()) is None
    assert await store.list_active(user, NOW + timedelta(days=30)) == []
