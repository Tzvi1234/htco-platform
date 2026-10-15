"""A shop's connection to its marketplace (Phase 4): which provider, and whether it works."""

from enum import StrEnum


class IntegrationProvider(StrEnum):
    ETSY = "etsy"


class IntegrationStatus(StrEnum):
    CONNECTED = "connected"  # tokens stored; calls may be made
    NEEDS_REAUTH = "needs_reauth"  # refresh refused or failed repeatedly; the owner must reconnect
    DISCONNECTED = "disconnected"  # tokens deleted; no call is made for this shop
