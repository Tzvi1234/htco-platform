"""The Etsy token vault against real Postgres (slice 4.2).

Every test creates its own organization and shop, and seals with key IDs of its own, so rotation
in one test never touches another test's rows.
"""

import base64
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.shop_integrations import EtsyConnection, OAuthTokens, TokenVaultError
from htco_domain.integrations import IntegrationStatus
from htco_persistence.database import Database
from htco_persistence.shop_integrations import PostgresTokenVault
from htco_platform.crypto import EnvelopeSealer, KeyRing
from tests.log_capture import capture_logs

pytestmark = [pytest.mark.db, pytest.mark.anyio]

NOW = datetime.now(UTC).replace(microsecond=0)


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


def key_id(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


def vault(database: Database, current: str, *previous: str) -> PostgresTokenVault:
    keys = {name: os.urandom(32) for name in (current, *previous)}
    _KEYS.update(keys)
    ring = KeyRing((current, _KEYS[current]), [(name, _KEYS[name]) for name in previous])
    return PostgresTokenVault(database, EnvelopeSealer(ring))


# Key material per key ID for this test run, so two vaults can share a key the way two deployed
# configurations share a secret.
_KEYS: dict[str, bytes] = {}


def vault_with(database: Database, current: str, *previous: str) -> PostgresTokenVault:
    ring = KeyRing((current, _KEYS[current]), [(name, _KEYS[name]) for name in previous])
    return PostgresTokenVault(database, EnvelopeSealer(ring))


def new_shop(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    organization_id, shop_id = uuid.uuid4(), uuid.uuid4()
    with database.connection() as connection:
        connection.execute(
            "insert into app.organizations (id, name) values (%s, %s)",
            [organization_id, "Vault test organization"],
        )
        connection.execute(
            """
            insert into app.shops (id, organization_id, name, marketplace, status)
            values (%s, %s, 'Vault test shop', 'etsy', 'pending')
            """,
            [shop_id, organization_id],
        )
    return organization_id, shop_id


def unique_tokens(label: str = "") -> OAuthTokens:
    marker = uuid.uuid4().hex
    return OAuthTokens(
        access_token=f"98765432.access{label}-{marker}",
        refresh_token=f"98765432.refresh{label}-{marker}",
        access_expires_at=NOW + timedelta(hours=1),
        refresh_expires_at=NOW + timedelta(days=90),
    )


def connection_for(
    shop: tuple[uuid.UUID, uuid.UUID], tokens: OAuthTokens, scopes: frozenset[str] | None = None
) -> EtsyConnection:
    return EtsyConnection(
        organization_id=shop[0],
        shop_id=shop[1],
        etsy_shop_id=4242,
        etsy_user_id=7777,
        scopes=scopes or frozenset({"shops_r", "transactions_r", "listings_r"}),
        tokens=tokens,
    )


def row_text(database: Database, shop_id: uuid.UUID) -> str:
    with database.connection() as connection:
        row = connection.execute(
            "select t::text from app.shop_integrations t where shop_id = %s", [shop_id]
        ).fetchone()
    assert row is not None
    text: str = row[0]
    return text


def leaked(text: str, tokens: OAuthTokens) -> list[str]:
    """Each form a token could take in the row's text: plain, hex (bytea output) or base64."""
    forms = [
        form
        for token in (tokens.access_token, tokens.refresh_token)
        for form in (token, token.encode().hex(), base64.b64encode(token.encode()).decode())
    ]
    return [form for form in forms if form in text]


@pytest.mark.ac("AC-4.2.1")
async def test_store_then_load__round_trips_and_the_row_holds_no_plaintext_token(
    database: Database,
) -> None:
    subject = vault(database, key_id("a"))
    shop = new_shop(database)
    tokens = unique_tokens()

    stored = await subject.store(connection_for(shop, tokens))
    loaded = await subject.load(shop[1])

    assert loaded is not None
    assert loaded == stored
    assert loaded.tokens == tokens
    assert (loaded.status, loaded.failure_count, loaded.etsy_shop_id) == (
        IntegrationStatus.CONNECTED,
        0,
        4242,
    )
    assert loaded.scopes == frozenset({"shops_r", "transactions_r", "listings_r"})
    assert leaked(row_text(database, shop[1]), tokens) == []


async def test_load__unknown_shop__is_none(database: Database) -> None:
    assert await vault(database, key_id("a")).load(uuid.uuid4()) is None


@pytest.mark.ac("AC-4.2.1")
async def test_store__reconnecting__updates_the_one_row_with_new_tokens_and_scopes(
    database: Database,
) -> None:
    subject = vault(database, key_id("a"))
    shop = new_shop(database)
    first = await subject.store(connection_for(shop, unique_tokens("1")))
    await subject.record_refresh_failure(shop[1])
    await subject.mark_needs_reauth(shop[1])
    newer = unique_tokens("2")

    second = await subject.store(
        connection_for(shop, newer, frozenset({"shops_r", "transactions_r", "transactions_w"}))
    )

    assert second.id == first.id
    assert second.tokens == newer
    assert second.status is IntegrationStatus.CONNECTED
    assert second.failure_count == 0
    assert "transactions_w" in second.scopes
    with database.connection() as connection:
        row = connection.execute(
            "select count(*) from app.shop_integrations where shop_id = %s", [shop[1]]
        ).fetchone()
    assert row == (1,)
    assert leaked(row_text(database, shop[1]), newer) == []


async def test_store__existing_shop_with_another_organization__is_refused_and_changes_nothing(
    database: Database,
) -> None:
    """Codex review on #43: the upsert must not let another tenant overwrite a shop's row."""
    subject = vault(database, key_id("a"))
    shop = new_shop(database)
    other_organization = new_shop(database)[0]
    owned = unique_tokens("owned")
    await subject.store(connection_for(shop, owned))

    with pytest.raises(TokenVaultError, match=str(shop[1])):
        await subject.store(connection_for((other_organization, shop[1]), unique_tokens("x")))

    loaded = await subject.load(shop[1])
    assert loaded is not None
    assert loaded.organization_id == shop[0]
    assert loaded.tokens == owned


async def test_update_tokens__after_a_refresh__stores_the_newest_and_resets_failures(
    database: Database,
) -> None:
    subject = vault(database, key_id("a"))
    shop = new_shop(database)
    await subject.store(connection_for(shop, unique_tokens("1")))
    assert await subject.record_refresh_failure(shop[1]) == 1
    assert await subject.record_refresh_failure(shop[1]) == 2
    refreshed = unique_tokens("2")

    assert await subject.update_tokens(shop[1], refreshed) is True

    loaded = await subject.load(shop[1])
    assert loaded is not None
    assert loaded.tokens == refreshed
    assert loaded.failure_count == 0
    assert leaked(row_text(database, shop[1]), refreshed) == []


async def test_record_successful_call__keeps_the_time(database: Database) -> None:
    subject = vault(database, key_id("a"))
    shop = new_shop(database)
    await subject.store(connection_for(shop, unique_tokens()))

    await subject.record_successful_call(shop[1], at=NOW)

    loaded = await subject.load(shop[1])
    assert loaded is not None
    assert loaded.last_successful_call_at == NOW


async def test_mark_needs_reauth__stops_the_tokens_being_usable(database: Database) -> None:
    subject = vault(database, key_id("a"))
    shop = new_shop(database)
    await subject.store(connection_for(shop, unique_tokens()))

    await subject.mark_needs_reauth(shop[1])

    loaded = await subject.load(shop[1])
    assert loaded is not None
    assert loaded.status is IntegrationStatus.NEEDS_REAUTH
    assert not loaded.is_usable


async def test_disconnect__deletes_the_tokens_and_a_late_refresh_cannot_bring_them_back(
    database: Database,
) -> None:
    subject = vault(database, key_id("a"))
    shop = new_shop(database)
    tokens = unique_tokens()
    await subject.store(connection_for(shop, tokens))

    await subject.disconnect(shop[1])
    late = await subject.update_tokens(shop[1], unique_tokens("late"))

    assert late is False
    loaded = await subject.load(shop[1])
    assert loaded is not None
    assert loaded.status is IntegrationStatus.DISCONNECTED
    assert loaded.tokens is None
    assert loaded.key_id is None
    with database.connection() as connection:
        row = connection.execute(
            """
            select key_id, wrapped_data_key, access_token_ciphertext, refresh_token_ciphertext
            from app.shop_integrations where shop_id = %s
            """,
            [shop[1]],
        ).fetchone()
    assert row == (None, None, None, None)


@pytest.mark.ac("AC-4.2.3")
async def test_load__ciphertext_moved_to_another_shops_row__fails_without_showing_a_token(
    database: Database,
) -> None:
    subject = vault(database, key_id("a"))
    victim, attacker = new_shop(database), new_shop(database)
    victim_tokens = unique_tokens("victim")
    await subject.store(connection_for(victim, victim_tokens))
    await subject.store(connection_for(attacker, unique_tokens("attacker")))
    with database.connection() as connection:
        connection.execute(
            """
            update app.shop_integrations a
            set key_id = v.key_id, wrapped_data_key = v.wrapped_data_key,
                access_token_ciphertext = v.access_token_ciphertext,
                refresh_token_ciphertext = v.refresh_token_ciphertext
            from app.shop_integrations v
            where a.shop_id = %s and v.shop_id = %s
            """,
            [attacker[1], victim[1]],
        )

    with pytest.raises(TokenVaultError) as raised:
        await subject.load(attacker[1])

    assert victim_tokens.access_token not in str(raised.value)
    assert victim_tokens.access_token not in repr(raised.value)


def key_ids_of(database: Database, shop_ids: list[uuid.UUID]) -> list[str]:
    with database.connection() as connection:
        rows = connection.execute(
            "select key_id from app.shop_integrations where shop_id = any(%s) order by key_id",
            [shop_ids],
        ).fetchall()
    return [row[0] for row in rows]


@pytest.mark.ac("AC-4.2.2")
async def test_rotation__rows_on_key_a_stay_readable_while_it_runs_and_end_on_key_b(
    database: Database,
) -> None:
    key_a, key_b = key_id("a"), key_id("b")
    writer = vault(database, key_a)
    _KEYS[key_b] = os.urandom(32)
    shops = [new_shop(database) for _ in range(3)]
    tokens = [unique_tokens(str(n)) for n in range(3)]
    for shop, issued in zip(shops, tokens, strict=True):
        await writer.store(connection_for(shop, issued))
    shop_ids = [shop[1] for shop in shops]
    rotating = vault_with(database, key_b, key_a)

    # Part way: one row moved to B, the rest still on A, and every row opens for both the
    # rotating configuration and the one still running with key A alone (for rows on A).
    assert await rotating.rotate_batch(limit=1) == 1
    assert sorted(key_ids_of(database, shop_ids)) == sorted([key_a, key_a, key_b])
    for shop, issued in zip(shops, tokens, strict=True):
        loaded = await rotating.load(shop[1])
        assert loaded is not None
        assert loaded.tokens == issued
        if loaded.key_id == key_a:
            still_on_a = await writer.load(shop[1])
            assert still_on_a is not None
            assert still_on_a.tokens == issued

    # A refresh during rotation writes with the current key.
    refreshed = unique_tokens("refreshed")
    assert await rotating.update_tokens(shop_ids[2], refreshed) is True
    tokens[2] = refreshed

    rotated = await rotating.rotate_keys(batch_size=1)

    assert rotated == 1  # shop 0 moved in the first batch, shop 2 by its refresh
    assert key_ids_of(database, shop_ids) == [key_b, key_b, key_b]
    only_b = vault_with(database, key_b)  # key A can now be removed
    for shop, issued in zip(shops, tokens, strict=True):
        loaded = await only_b.load(shop[1])
        assert loaded is not None
        assert loaded.tokens == issued
        assert leaked(row_text(database, shop[1]), issued) == []
    assert await rotating.rotate_batch(limit=10) == 0


@pytest.mark.ac("AC-4.2.2")
async def test_key_usage__counts_rows_per_key_id(database: Database) -> None:
    key_a = key_id("a")
    subject = vault(database, key_a)
    for _ in range(2):
        await subject.store(connection_for(new_shop(database), unique_tokens()))

    usage = await subject.key_usage()

    assert usage[key_a] == 2


@pytest.mark.ac("AC-4.2.3")
async def test_vault_operations__never_log_a_token(database: Database) -> None:
    key_a, key_b = key_id("a"), key_id("b")
    shop = new_shop(database)
    tokens, refreshed = unique_tokens("1"), unique_tokens("2")

    with capture_logs("DEBUG") as logs:
        subject = vault(database, key_a)
        await subject.store(connection_for(shop, tokens))
        await subject.load(shop[1])
        await subject.update_tokens(shop[1], refreshed)
        await subject.record_refresh_failure(shop[1])
        _KEYS[key_b] = os.urandom(32)
        rotating = vault_with(database, key_b, key_a)
        await rotating.rotate_batch(limit=10)
        await rotating.mark_needs_reauth(shop[1])
        await rotating.disconnect(shop[1])

    raw = logs.raw()
    assert leaked(raw, tokens) == []
    assert leaked(raw, refreshed) == []
    for key in (_KEYS[key_a], _KEYS[key_b]):
        assert base64.b64encode(key).decode() not in raw
