"""A shop's connection to its marketplace (Phase 4): which provider, and whether it works."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

# The shop card warns this long before the refresh token expires (Etsy: 90 days), so the owner
# can reconnect before calls stop (slice 4.4).
REFRESH_EXPIRY_WARNING = timedelta(days=10)


class IntegrationProvider(StrEnum):
    ETSY = "etsy"


class IntegrationStatus(StrEnum):
    CONNECTED = "connected"  # tokens stored; calls may be made
    NEEDS_REAUTH = "needs_reauth"  # refresh refused or failed repeatedly; the owner must reconnect
    DISCONNECTED = "disconnected"  # tokens deleted; no call is made for this shop


class TokenHealth(StrEnum):
    OK = "ok"
    EXPIRING = "expiring"  # the refresh token ends within REFRESH_EXPIRY_WARNING: reconnect soon
    NEEDS_REAUTH = "needs_reauth"  # calls cannot be made until the owner reconnects
    DISCONNECTED = "disconnected"


def token_health(
    status: IntegrationStatus, refresh_expires_at: datetime | None, *, now: datetime
) -> TokenHealth:
    """What the shop card shows about the connection's tokens."""
    if status is IntegrationStatus.DISCONNECTED:
        return TokenHealth.DISCONNECTED
    if (
        status is IntegrationStatus.NEEDS_REAUTH
        or refresh_expires_at is None
        or refresh_expires_at <= now
    ):
        return TokenHealth.NEEDS_REAUTH
    if refresh_expires_at - now <= REFRESH_EXPIRY_WARNING:
        return TokenHealth.EXPIRING
    return TokenHealth.OK


# --- Capabilities (slice 4.5) ------------------------------------------------------------------


class ShopCapability(StrEnum):
    """What HTCO checks it may do with a connected shop, in the order the shop card shows them."""

    GRANTED_SCOPES = "granted_scopes"  # the scopes HTCO asked for were granted
    SHOP_DETAILS = "shop_details"  # the shop itself can be read
    RECEIPTS_READ = "receipts_read"  # orders (receipts) can be read
    BUYER_ADDRESS = "buyer_address"  # receipts carry the buyer's shipping address
    LISTINGS_READ = "listings_read"  # listings can be read
    WEBHOOKS = "webhooks"  # Etsy pushes order events to HTCO
    SHIPMENT_WRITE = "shipment_write"  # tracking could be uploaded (never probed: a write)


class CapabilityState(StrEnum):
    AVAILABLE = "available"  # a probe succeeded and showed it works
    UNAVAILABLE = "unavailable"  # a probe succeeded and showed it does not
    UNKNOWN = "unknown"  # no successful probe says either way


def capability_state(*, probed_ok: bool, present: bool) -> CapabilityState:
    """A capability is available only after a successful probe found it (AC-4.5.1)."""
    if not probed_ok:
        return CapabilityState.UNKNOWN
    return CapabilityState.AVAILABLE if present else CapabilityState.UNAVAILABLE


# --- Disconnect (slice 4.7) --------------------------------------------------------------------

# A disconnected shop's data is purged this long after the disconnect (the purge job runs from
# Phase 5; records under legal retention are kept). Reconnecting before then cancels the purge.
DISCONNECTED_DATA_RETENTION = timedelta(days=30)


def purge_after(disconnected_at: datetime) -> datetime:
    if disconnected_at.tzinfo is None or disconnected_at.utcoffset() is None:
        raise ValueError("the disconnect time must carry a timezone")
    return disconnected_at + DISCONNECTED_DATA_RETENTION


_DETAIL = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class CapabilityResult:
    """One probed capability of a shop. `detail` is a short machine code (why this state), never
    free text: nothing a marketplace answered, and no buyer data, is ever stored with it."""

    capability: ShopCapability
    state: CapabilityState
    detail: str
    checked_at: datetime

    def __post_init__(self) -> None:
        if not _DETAIL.fullmatch(self.detail):
            raise ValueError("a capability detail must be a short lower-case code")
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must carry a timezone")
