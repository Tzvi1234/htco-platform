"""Capability probes (slice 4.5): what HTCO may do with a connected shop, checked by reading only.

Run when a shop is connected and on demand ("Re-check" on the shop card; daily runs arrive with
the job scheduler in Phase 5). Every probe is a GET: HTCO never writes to a shop (the owner's
rule for live shops with real buyers; the Etsy adapter refuses any other method). A capability is
`available` only after a successful read showed it works (AC-4.5.1):

- `shop_details`: getShop (`shops_r`).
- `receipts_read`: one receipt (`limit=1`, `transactions_r`).
- `buyer_address`: whether that receipt's address fields hold anything. Presence only: the
  values are never kept, returned or logged.
- `listings_read`: one active listing (`limit=1`, `listings_r`).
- `granted_scopes`: the stored scopes, each verified by the read that needs it (Etsy's
  `tokenScopes` is a POST, so it is not called).
- `webhooks`: configuration (Etsy has no API to ask); `unknown` at best until Phase 5 receives
  one.
- `shipment_write`: never probed. No safe probe exists without creating a shipment, so it stays
  `unknown` and writes stay disabled.

A scope that was not granted makes its capability `unavailable` without a call. A 401 stops the
remaining reads (the token is the problem, not the capability). Results are stored per shop with
`checked_at`, the integration's `last_successful_call_at` is set when any read succeeded, and the
check is audited with the states only. Details are short codes, never anything Etsy answered.
"""

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.access.authentication import AuthContext, NoMembershipError
from htco_application.access.policy import Capability, Outcome, Scope, ShopRef, decide
from htco_application.audit import AuditRecord
from htco_application.marketplace import (
    Listing,
    ListingState,
    MarketplaceAuthError,
    MarketplaceError,
    MarketplaceNotFoundError,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceResponseError,
    MarketplaceShop,
    MarketplaceUnavailableError,
    Page,
    QuotaDeferredError,
    ReceiptAddressCheck,
)
from htco_application.shop_integrations import ShopIntegration
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.integrations import CapabilityResult, CapabilityState, ShopCapability

# A re-check calls Etsy three times; the owner's button may not be pressed faster than this.
RECHECK_COOLDOWN = timedelta(seconds=30)

_SHOPS, _TRANSACTIONS, _LISTINGS = "shops_r", "transactions_r", "listings_r"
_READS = (
    (ShopCapability.SHOP_DETAILS, _SHOPS),
    (ShopCapability.RECEIPTS_READ, _TRANSACTIONS),
    (ShopCapability.LISTINGS_READ, _LISTINGS),
)


class ShopNotConnectedError(Exception):
    """The shop's connection is not usable (needs re-authentication or disconnected): no probe
    runs and no marketplace call is made."""


class RecheckTooSoonError(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"checked moments ago; try again in {retry_after_seconds} s")
        self.retry_after_seconds = retry_after_seconds


class CapabilityProbe(Protocol):
    """The marketplace reads the probes make (the Etsy adapter)."""

    async def get_shop(self, shop_id: int, *, urgent: bool = False) -> MarketplaceShop: ...

    async def check_receipt_addresses(
        self, shop_id: int, *, urgent: bool = True
    ) -> ReceiptAddressCheck: ...

    async def list_listings(
        self,
        shop_id: int,
        *,
        state: ListingState = ListingState.ACTIVE,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Listing]: ...


class ProbedConnections(Protocol):
    """The part of the token vault the probes use."""

    async def load(self, shop_id: uuid.UUID) -> ShopIntegration | None: ...

    async def record_successful_call(self, shop_id: uuid.UUID, *, at: datetime) -> None: ...


class CapabilityStore(Protocol):
    async def save(
        self,
        organization_id: uuid.UUID,
        shop_id: uuid.UUID,
        results: Sequence[CapabilityResult],
        *,
        generation: int,
    ) -> bool:
        """Keep the latest result per shop and capability, only while the shop is still connected
        with the same connection `generation` the probe started under. False: dropped (the shop
        was disconnected or reconnected meanwhile)."""
        ...

    async def reserve_check(
        self, shop_id: uuid.UUID, *, now: datetime, cooldown: timedelta
    ) -> timedelta | None:
        """Atomically claim the shop's next re-check: None when claimed (no check started within
        `cooldown`), otherwise how long to wait. Concurrent callers have exactly one winner."""
        ...


class AuditAppender(Protocol):
    async def append(self, record: AuditRecord) -> None: ...


@dataclass(frozen=True)
class CapabilityChecks:
    """The probes' collaborators, built by the composition root."""

    vault: ProbedConnections
    marketplace: CapabilityProbe
    store: CapabilityStore
    audit: AuditAppender
    # True once Etsy webhooks are set up (Phase 5); configuration alone never makes them available.
    webhooks_configured: bool = False


# --- Use cases ---------------------------------------------------------------------------------


async def probe_capabilities(  # noqa: PLR0913 - the shop, who asked, and why
    checks: CapabilityChecks,
    integration: ShopIntegration,
    *,
    now: datetime,
    actor: ActorRef,
    trigger: str,
    correlation_id: str | None,
) -> tuple[CapabilityResult, ...]:
    """Probe one connected shop, store and audit the results. Raises `ShopNotConnectedError`
    (without any marketplace call) unless the connection is usable."""
    if not integration.is_usable:
        raise ShopNotConnectedError(f"shop {integration.shop_id} is not connected")
    results = await _probe(checks, integration, now)
    if any(r.state is CapabilityState.AVAILABLE and r.detail == "read_ok" for r in results):
        await checks.vault.record_successful_call(integration.shop_id, at=now)
    await checks.store.save(
        integration.organization_id,
        integration.shop_id,
        results,
        generation=integration.generation,
    )
    await checks.audit.append(
        AuditRecord.new(
            organization_id=integration.organization_id,
            actor=actor,
            action="shop.capabilities_checked",
            entity_type="shop",
            entity_id=str(integration.shop_id),
            after={
                "trigger": trigger,
                "states": {r.capability.value: r.state.value for r in results},
            },
            correlation_id=correlation_id,
        )
    )
    return results


async def recheck_capabilities(
    checks: CapabilityChecks,
    auth: AuthContext,
    shop_id: uuid.UUID,
    *,
    now: datetime,
    correlation_id: str | None,
) -> tuple[CapabilityResult, ...]:
    """The shop card's "Re-check": Owner and Admin (organization-wide roles, like the connection
    health they see). Raises `ShopNotFoundError`, `ShopAccessDeniedError`,
    `ShopNotConnectedError` or `RecheckTooSoonError`."""
    integration = await checks.vault.load(shop_id)
    if integration is None:
        raise ShopNotFoundError("no such shop connection")
    try:
        principal = auth.principal_for(integration.organization_id)
    except NoMembershipError:
        raise ShopNotFoundError("no such shop connection") from None
    shop = ShopRef(id=shop_id, organization_id=integration.organization_id)
    decision = decide(principal, Capability.VIEW_OVERVIEW, shop=shop, now=now)
    if decision.outcome is not Outcome.ALLOW or decision.scope is not Scope.ORGANIZATION:
        raise ShopAccessDeniedError("only organization-wide roles re-check a connection")
    if not integration.is_usable:
        raise ShopNotConnectedError(f"shop {shop_id} is not connected")
    # Claimed before probing, so two presses at once cannot both call Etsy (Codex review of #49).
    wait = await checks.store.reserve_check(shop_id, now=now, cooldown=RECHECK_COOLDOWN)
    if wait is not None:
        raise RecheckTooSoonError(max(1, math.ceil(wait.total_seconds())))
    return await probe_capabilities(
        checks,
        integration,
        now=now,
        actor=ActorRef(ActorKind.USER, str(auth.user_id)),
        trigger="recheck",
        correlation_id=correlation_id,
    )


# --- The probes --------------------------------------------------------------------------------


def _failure(error: MarketplaceError) -> tuple[CapabilityState, str]:
    """What a failed read says: only Etsy's explicit refusal (403) means `unavailable`."""
    if isinstance(error, MarketplacePermissionError):
        return CapabilityState.UNAVAILABLE, "refused_by_etsy"
    for kinds, detail in _UNKNOWN_BECAUSE:
        if isinstance(error, kinds):
            return CapabilityState.UNKNOWN, detail
    return CapabilityState.UNKNOWN, "etsy_error"


_UNKNOWN_BECAUSE: tuple[tuple[tuple[type[MarketplaceError], ...], str], ...] = (
    ((MarketplaceAuthError,), "auth_failed"),
    ((MarketplaceNotFoundError,), "not_found"),
    ((MarketplaceRateLimitedError, QuotaDeferredError), "rate_limited"),
    ((MarketplaceUnavailableError,), "etsy_unreachable"),
    ((MarketplaceResponseError,), "unexpected_answer"),
)


async def _probe(
    checks: CapabilityChecks, integration: ShopIntegration, now: datetime
) -> tuple[CapabilityResult, ...]:
    etsy, scopes = integration.etsy_shop_id, integration.scopes
    market = checks.marketplace
    found: dict[ShopCapability, tuple[CapabilityState, str]] = {}
    token_refused = False
    address: tuple[CapabilityState, str] = (CapabilityState.UNKNOWN, "receipts_unreadable")
    for capability, scope in _READS:
        if scope not in scopes:
            found[capability] = (CapabilityState.UNAVAILABLE, "scope_missing")
            if capability is ShopCapability.RECEIPTS_READ:
                address = (CapabilityState.UNAVAILABLE, "scope_missing")
            continue
        if token_refused:
            found[capability] = (CapabilityState.UNKNOWN, "auth_failed")
            if capability is ShopCapability.RECEIPTS_READ:
                address = (CapabilityState.UNKNOWN, "auth_failed")
            continue
        try:
            if capability is ShopCapability.SHOP_DETAILS:
                await market.get_shop(etsy, urgent=True)
            elif capability is ShopCapability.RECEIPTS_READ:
                check = await market.check_receipt_addresses(etsy, urgent=True)
                address = _address(check)
            else:
                await market.list_listings(etsy, limit=1, urgent=True)
        except MarketplaceError as error:
            found[capability] = _failure(error)
            token_refused = isinstance(error, MarketplaceAuthError)
            if token_refused and capability is ShopCapability.RECEIPTS_READ:
                address = (CapabilityState.UNKNOWN, "auth_failed")
            continue
        found[capability] = (CapabilityState.AVAILABLE, "read_ok")
    found[ShopCapability.BUYER_ADDRESS] = address
    found[ShopCapability.GRANTED_SCOPES] = _scopes(found)
    found[ShopCapability.WEBHOOKS] = (
        (CapabilityState.UNKNOWN, "configured_not_verified")
        if checks.webhooks_configured
        else (CapabilityState.UNAVAILABLE, "not_configured")
    )
    found[ShopCapability.SHIPMENT_WRITE] = (CapabilityState.UNKNOWN, "no_safe_probe")
    return tuple(
        CapabilityResult(capability, *found[capability], checked_at=now)
        for capability in ShopCapability
    )


def _address(check: ReceiptAddressCheck) -> tuple[CapabilityState, str]:
    if check.address_present is None:
        return CapabilityState.UNKNOWN, "no_receipts_yet"
    if check.address_present:
        return CapabilityState.AVAILABLE, "address_fields_present"
    return CapabilityState.UNAVAILABLE, "address_fields_hidden"


def _scopes(
    found: dict[ShopCapability, tuple[CapabilityState, str]],
) -> tuple[CapabilityState, str]:
    """Granted scopes are verified by the reads that need them, not taken on trust."""
    reads = [found[capability] for capability, _ in _READS]
    if any(detail == "scope_missing" for _, detail in reads):
        return CapabilityState.UNAVAILABLE, "scope_missing"
    if any(detail == "refused_by_etsy" for _, detail in reads):
        return CapabilityState.UNAVAILABLE, "refused_by_etsy"
    if all(state is CapabilityState.AVAILABLE for state, _ in reads):
        return CapabilityState.AVAILABLE, "verified_by_reads"
    return CapabilityState.UNKNOWN, "not_verified"
