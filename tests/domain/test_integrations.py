"""Token health of a shop's marketplace connection (slice 4.4): what the shop card warns about."""

from datetime import UTC, datetime, timedelta

import pytest

from htco_domain.integrations import (
    REFRESH_EXPIRY_WARNING,
    IntegrationStatus,
    TokenHealth,
    token_health,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def test_token_health__connected_with_a_distant_refresh_expiry__ok() -> None:
    health = token_health(IntegrationStatus.CONNECTED, NOW + timedelta(days=60), now=NOW)

    assert health is TokenHealth.OK


@pytest.mark.parametrize("left", [timedelta(days=10), timedelta(days=3), timedelta(seconds=1)])
def test_token_health__refresh_token_expires_within_ten_days__expiring(left: timedelta) -> None:
    health = token_health(IntegrationStatus.CONNECTED, NOW + left, now=NOW)

    assert timedelta(days=10) == REFRESH_EXPIRY_WARNING
    assert health is TokenHealth.EXPIRING


def test_token_health__one_second_before_the_warning_window__still_ok() -> None:
    expires = NOW + REFRESH_EXPIRY_WARNING + timedelta(seconds=1)

    assert token_health(IntegrationStatus.CONNECTED, expires, now=NOW) is TokenHealth.OK


@pytest.mark.parametrize("expired", [NOW, NOW - timedelta(days=1)])
def test_token_health__refresh_token_expired__needs_reauth(expired: datetime) -> None:
    assert token_health(IntegrationStatus.CONNECTED, expired, now=NOW) is TokenHealth.NEEDS_REAUTH


def test_token_health__marked_needs_reauth__needs_reauth_whatever_the_expiry() -> None:
    health = token_health(IntegrationStatus.NEEDS_REAUTH, NOW + timedelta(days=80), now=NOW)

    assert health is TokenHealth.NEEDS_REAUTH


def test_token_health__disconnected__disconnected() -> None:
    assert token_health(IntegrationStatus.DISCONNECTED, None, now=NOW) is TokenHealth.DISCONNECTED


def test_token_health__connected_without_an_expiry__needs_reauth() -> None:
    """A connected row always has an expiry (database check); missing data is not trusted."""
    assert token_health(IntegrationStatus.CONNECTED, None, now=NOW) is TokenHealth.NEEDS_REAUTH
