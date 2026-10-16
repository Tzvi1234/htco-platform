"""Token health of a shop's marketplace connection (slice 4.4): what the shop card warns about."""

from datetime import UTC, datetime, timedelta

import pytest

from htco_domain.integrations import (
    DISCONNECTED_DATA_RETENTION,
    REFRESH_EXPIRY_WARNING,
    CapabilityResult,
    CapabilityState,
    IntegrationStatus,
    ShopCapability,
    TokenHealth,
    capability_state,
    purge_after,
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


# --- Capabilities and the purge date (slices 4.5 and 4.7) ------------------------------------


def test_capability_state__never_available_without_a_successful_probe() -> None:
    assert capability_state(probed_ok=True, present=True) is CapabilityState.AVAILABLE
    assert capability_state(probed_ok=True, present=False) is CapabilityState.UNAVAILABLE
    assert capability_state(probed_ok=False, present=True) is CapabilityState.UNKNOWN
    assert capability_state(probed_ok=False, present=False) is CapabilityState.UNKNOWN


def test_shop_capabilities__are_the_probed_ones_in_display_order() -> None:
    assert [c.value for c in ShopCapability] == [
        "granted_scopes",
        "shop_details",
        "receipts_read",
        "buyer_address",
        "listings_read",
        "webhooks",
        "shipment_write",
    ]


def test_purge_after__is_thirty_days_after_the_disconnect() -> None:
    assert timedelta(days=30) == DISCONNECTED_DATA_RETENTION
    assert purge_after(NOW) == NOW + timedelta(days=30)


def test_purge_after__needs_a_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        purge_after(datetime(2026, 10, 15, 12, 0))  # noqa: DTZ001 - the case under test


def test_capability_result__keeps_a_machine_detail_and_an_aware_time() -> None:
    result = CapabilityResult(
        ShopCapability.WEBHOOKS, CapabilityState.UNAVAILABLE, "not_configured", NOW
    )

    assert result.detail == "not_configured"


@pytest.mark.parametrize("detail", ["", "Has Spaces", "x" * 65, "addr: 1 Main St"])
def test_capability_result__free_text_detail__refused(detail: str) -> None:
    """Details are codes, never text that could carry buyer data."""
    with pytest.raises(ValueError, match="detail"):
        CapabilityResult(ShopCapability.WEBHOOKS, CapabilityState.UNKNOWN, detail, NOW)


def test_capability_result__naive_time__refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        CapabilityResult(
            ShopCapability.WEBHOOKS,
            CapabilityState.UNKNOWN,
            "x",
            datetime(2026, 10, 15, 12, 0),  # noqa: DTZ001 - the case under test
        )
