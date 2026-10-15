"""The token vault's values (slice 4.2): tokens never show in a repr, a string or an error."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from htco_application.shop_integrations import (
    EtsyConnection,
    OAuthTokens,
    ShopIntegration,
    TokenVaultError,
)
from htco_domain.integrations import IntegrationProvider, IntegrationStatus

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("a3000000-0000-0000-0000-000000000001")
ACCESS = "12345678.access-token-value-for-tests"
REFRESH = "12345678.refresh-token-value-for-tests"


def tokens() -> OAuthTokens:
    return OAuthTokens(
        access_token=ACCESS,
        refresh_token=REFRESH,
        access_expires_at=NOW + timedelta(hours=1),
        refresh_expires_at=NOW + timedelta(days=90),
    )


def connection(**overrides: object) -> EtsyConnection:
    values: dict[str, object] = {
        "organization_id": ORG,
        "shop_id": SHOP,
        "etsy_shop_id": 4242,
        "etsy_user_id": 7777,
        "scopes": frozenset({"shops_r", "transactions_r"}),
        "tokens": tokens(),
    }
    return EtsyConnection(**(values | overrides))  # type: ignore[arg-type]


def integration() -> ShopIntegration:
    return ShopIntegration(
        id=uuid.UUID("a5000000-0000-0000-0000-000000000001"),
        organization_id=ORG,
        shop_id=SHOP,
        provider=IntegrationProvider.ETSY,
        etsy_shop_id=4242,
        etsy_user_id=7777,
        scopes=frozenset({"shops_r"}),
        status=IntegrationStatus.CONNECTED,
        tokens=tokens(),
        key_id="key-a",
        failure_count=0,
        last_successful_call_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.ac("AC-4.2.3")
def test_reprs__never_show_a_token() -> None:
    shown = " ".join(
        text
        for value in (tokens(), connection(), integration())
        for text in (repr(value), str(value))
    )

    assert ACCESS not in shown
    assert REFRESH not in shown
    assert "4242" in repr(connection())  # the rest stays visible for debugging


@pytest.mark.ac("AC-4.2.3")
def test_vault_error__carries_no_token() -> None:
    error = TokenVaultError.unreadable(SHOP)

    assert str(SHOP) in str(error)
    assert ACCESS not in str(error)


@pytest.mark.parametrize(
    "overrides",
    [{"etsy_shop_id": 0}, {"etsy_user_id": -1}, {"scopes": frozenset({" "})}],
    ids=["shop-id", "user-id", "blank-scope"],
)
def test_connection__invalid_values__are_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"etsy|scope"):
        connection(**overrides)


def test_tokens__blank_values__are_refused_without_repeating_them() -> None:
    with pytest.raises(ValueError, match="token") as raised:
        OAuthTokens(" ", REFRESH, NOW, NOW)
    assert REFRESH not in str(raised.value)


def test_integration__tokens_are_usable_only_when_connected() -> None:
    connected = integration()
    assert connected.is_usable
    assert not replace(connected, status=IntegrationStatus.NEEDS_REAUTH).is_usable
    assert not replace(connected, status=IntegrationStatus.DISCONNECTED, tokens=None).is_usable


def test_vault_error__wrong_organization__names_the_shop_only() -> None:
    error = TokenVaultError.wrong_organization(SHOP)

    assert str(SHOP) in str(error)
    assert "another organization" in str(error)
