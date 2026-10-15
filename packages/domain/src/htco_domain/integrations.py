"""A shop's connection to its marketplace (Phase 4): which provider, and whether it works."""

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
