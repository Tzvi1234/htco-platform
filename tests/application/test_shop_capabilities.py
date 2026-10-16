"""Capability probes (slice 4.5): what HTCO may do with a connected shop, checked by reading only.

A capability shows as available only after a successful probe (AC-4.5.1). The probes read the
shop, one receipt and one listing; shipment write permission is never probed, because no safe
probe exists without writing (AC-4.5.2, the owner's read-only rule): it stays `unknown`.
"""

import uuid
from dataclasses import replace
from datetime import timedelta

import anyio
import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.marketplace import (
    MarketplaceAuthError,
    MarketplaceError,
    MarketplaceNotFoundError,
    MarketplacePermissionError,
    MarketplaceRateLimitedError,
    MarketplaceResponseError,
    MarketplaceShop,
    MarketplaceUnavailableError,
    QuotaDeferredError,
    ReceiptAddressCheck,
)
from htco_application.shop_capabilities import (
    RECHECK_COOLDOWN,
    CapabilityChecks,
    ProbeSupersededError,
    RecheckTooSoonError,
    ShopNotConnectedError,
    probe_capabilities,
    recheck_capabilities,
)
from htco_application.shop_integrations import EtsyConnection, OAuthTokens, ShopIntegration
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.integrations import (
    CapabilityResult,
    CapabilityState,
    IntegrationStatus,
    ShopCapability,
)
from tests.etsy_fakes import ETSY_SHOP, NOW, AuditLog, Capabilities, Pinning, Probe, Vault

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("b0000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("a3000000-0000-0000-0000-000000000001")
USER = uuid.UUID("a1000000-0000-0000-0000-000000000001")
ALL_SCOPES = frozenset({"shops_r", "transactions_r", "listings_r"})
ACTOR = ActorRef(ActorKind.USER, str(USER))


class World:
    def __init__(
        self,
        *,
        scopes: frozenset[str] = ALL_SCOPES,
        probe: Probe | None = None,
        webhooks_configured: bool = False,
    ) -> None:
        self.vault = Vault()
        self.probe = probe or Probe()
        self.store = Capabilities()
        self.audit = AuditLog()
        self.vault.add(
            EtsyConnection(
                organization_id=ORG,
                shop_id=SHOP,
                etsy_shop_id=ETSY_SHOP,
                etsy_user_id=12345678,
                scopes=scopes,
                tokens=OAuthTokens(
                    access_token="12345678.fake-access-token",  # noqa: S106 - fake
                    refresh_token="12345678.fake-refresh-token",  # noqa: S106 - fake
                    access_expires_at=NOW + timedelta(hours=1),
                    refresh_expires_at=NOW + timedelta(days=90),
                ),
            )
        )
        self.checks = CapabilityChecks(
            vault=self.vault,
            marketplace=self.probe,
            store=self.store,
            tokens=Pinning(),
            webhooks_configured=webhooks_configured,
        )

    @property
    def integration(self) -> ShopIntegration:
        return self.vault.rows[SHOP]

    async def probe_now(self) -> dict[ShopCapability, CapabilityResult]:
        results = await probe_capabilities(
            self.checks,
            self.integration,
            now=NOW,
            actor=ACTOR,
            trigger="connect",
            correlation_id="req-1",
        )
        return {result.capability: result for result in results}


def auth(role: Role = Role.OWNER, org: uuid.UUID = ORG) -> AuthContext:
    return AuthContext(
        user_id=USER,
        assurance=AssuranceLevel.AAL1,
        session_id=None,
        memberships=(Membership(org, role, frozenset({SHOP})),),
    )


def states(found: dict[ShopCapability, CapabilityResult]) -> dict[str, tuple[str, str]]:
    return {c.value: (r.state.value, r.detail) for c, r in found.items()}


# --- What the probes find ----------------------------------------------------------------------


@pytest.mark.ac("AC-4.5.1")
async def test_probe__everything_readable__available_except_webhooks_and_shipment_write() -> None:
    world = World()

    found = await world.probe_now()

    assert states(found) == {
        "granted_scopes": ("available", "verified_by_reads"),
        "shop_details": ("available", "read_ok"),
        "receipts_read": ("available", "read_ok"),
        "buyer_address": ("available", "address_fields_present"),
        "listings_read": ("available", "read_ok"),
        "webhooks": ("unavailable", "not_configured"),
        "shipment_write": ("unknown", "no_safe_probe"),
    }
    assert all(result.checked_at == NOW for result in found.values())
    assert list(found) == list(ShopCapability)


@pytest.mark.ac("AC-4.5.2")
async def test_probe__reads_one_receipt_and_one_listing_urgently_and_nothing_else() -> None:
    world = World()

    await world.probe_now()

    assert world.probe.calls == [
        f"get_shop:{ETSY_SHOP}:True",
        f"check_receipt_addresses:{ETSY_SHOP}:True",
        f"list_listings:{ETSY_SHOP}:active:1:0:True",
    ]


async def test_probe__saves_the_results_records_the_successful_call_and_audits() -> None:
    world = World()

    found = await world.probe_now()

    [(organization, shop, saved)] = world.store.saved
    assert (organization, shop) == (ORG, SHOP)
    assert world.store.generations == [world.integration.generation]
    assert saved == tuple(found.values())
    assert world.store.successful_calls == [NOW]
    # Written by the store in the same transaction as the results (Codex review of #49).
    assert world.audit.records == []
    [record] = world.store.audits
    assert record.action == "shop.capabilities_checked"
    assert record.entity_id == str(SHOP)
    assert record.actor == ACTOR
    assert record.correlation_id == "req-1"
    assert record.after == {
        "trigger": "connect",
        "states": {c.value: r.state.value for c, r in found.items()},
    }


async def test_probe__addresses_hidden__buyer_address_unavailable() -> None:
    world = World(probe=Probe(addresses=ReceiptAddressCheck(1, address_present=False)))

    found = await world.probe_now()

    assert states(found)["buyer_address"] == ("unavailable", "address_fields_hidden")
    assert states(found)["receipts_read"] == ("available", "read_ok")


async def test_probe__no_receipt_yet__buyer_address_unknown() -> None:
    world = World(probe=Probe(addresses=ReceiptAddressCheck(0, address_present=None)))

    found = await world.probe_now()

    assert states(found)["buyer_address"] == ("unknown", "no_receipts_yet")
    assert states(found)["receipts_read"] == ("available", "read_ok")


@pytest.mark.ac("AC-4.5.1")
async def test_probe__webhooks_configured__still_unknown_until_one_arrives() -> None:
    """Configuration alone is not a successful probe: never `available` from it (AC-4.5.1)."""
    world = World(webhooks_configured=True)

    found = await world.probe_now()

    assert states(found)["webhooks"] == ("unknown", "configured_not_verified")


async def test_probe__scope_missing__unavailable_without_calling_etsy_for_it() -> None:
    world = World(scopes=frozenset({"shops_r"}))

    found = await world.probe_now()

    assert states(found)["receipts_read"] == ("unavailable", "scope_missing")
    assert states(found)["buyer_address"] == ("unavailable", "scope_missing")
    assert states(found)["listings_read"] == ("unavailable", "scope_missing")
    assert states(found)["granted_scopes"] == ("unavailable", "scope_missing")
    assert world.probe.calls == [f"get_shop:{ETSY_SHOP}:True"]


@pytest.mark.ac("AC-4.5.1")
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MarketplacePermissionError("403", status=403), ("unavailable", "refused_by_etsy")),
        (MarketplaceNotFoundError("404", status=404), ("unknown", "not_found")),
        (MarketplaceRateLimitedError("429", retry_after_seconds=3), ("unknown", "rate_limited")),
        (QuotaDeferredError("low", remaining_today=1, floor=5), ("unknown", "rate_limited")),
        (MarketplaceUnavailableError("503", status=503), ("unknown", "etsy_unreachable")),
        (MarketplaceResponseError("garbage"), ("unknown", "unexpected_answer")),
        (MarketplaceError("other", status=418), ("unknown", "etsy_error")),
    ],
)
async def test_probe__a_failed_read__never_available(
    error: MarketplaceError, expected: tuple[str, str]
) -> None:
    world = World(probe=Probe(listings=error))

    found = await world.probe_now()

    assert states(found)["listings_read"] == expected
    assert states(found)["shop_details"] == ("available", "read_ok")
    granted = states(found)["granted_scopes"]
    assert granted[0] != "available"


async def test_probe__receipts_refused__address_unknown_and_scopes_refused() -> None:
    world = World(probe=Probe(addresses=MarketplacePermissionError("403", status=403)))

    found = await world.probe_now()

    assert states(found)["receipts_read"] == ("unavailable", "refused_by_etsy")
    assert states(found)["buyer_address"] == ("unknown", "receipts_unreadable")
    assert states(found)["granted_scopes"] == ("unavailable", "refused_by_etsy")


async def test_probe__token_refused__stops_after_the_first_call() -> None:
    world = World(probe=Probe(shop=MarketplaceAuthError("401", status=401)))

    found = await world.probe_now()

    assert world.probe.calls == [f"get_shop:{ETSY_SHOP}:True"]
    for capability in ("shop_details", "receipts_read", "buyer_address", "listings_read"):
        assert states(found)[capability] == ("unknown", "auth_failed")
    assert states(found)["granted_scopes"] == ("unknown", "not_verified")
    assert world.store.successful_calls == [None]


async def test_probe__no_successful_call__last_successful_call_untouched() -> None:
    down = MarketplaceUnavailableError("503", status=503)
    world = World(probe=Probe(shop=down, addresses=down, listings=down))

    found = await world.probe_now()

    assert all(r.state is not CapabilityState.AVAILABLE for r in found.values())
    assert world.store.successful_calls == [None]


@pytest.mark.parametrize("status", [IntegrationStatus.NEEDS_REAUTH, IntegrationStatus.DISCONNECTED])
async def test_probe__not_connected__refused_without_any_etsy_call(
    status: IntegrationStatus,
) -> None:
    world = World()
    world.vault.rows[SHOP] = replace(world.integration, status=status)

    with pytest.raises(ShopNotConnectedError):
        await world.probe_now()

    assert world.probe.calls == []
    assert world.store.saved == []


# --- Re-check on demand ------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
async def test_recheck__owner_or_admin__probes_and_answers_the_results(role: Role) -> None:
    world = World()

    results = await recheck_capabilities(
        world.checks, auth(role), SHOP, now=NOW, correlation_id="req-2"
    )

    assert [r.capability for r in results] == list(ShopCapability)
    [record] = world.store.audits
    assert record.after is not None
    assert record.after["trigger"] == "recheck"
    assert record.actor == ACTOR


@pytest.mark.parametrize("role", [Role.OPERATIONS, Role.SUPPORT, Role.VIEWER, Role.FINANCE])
async def test_recheck__other_roles__refused_without_any_etsy_call(role: Role) -> None:
    world = World()

    with pytest.raises(ShopAccessDeniedError):
        await recheck_capabilities(world.checks, auth(role), SHOP, now=NOW, correlation_id=None)

    assert world.probe.calls == []


async def test_recheck__shop_of_another_organization__not_found() -> None:
    world = World()

    with pytest.raises(ShopNotFoundError):
        await recheck_capabilities(
            world.checks, auth(org=OTHER_ORG), SHOP, now=NOW, correlation_id=None
        )

    assert world.probe.calls == []


async def test_recheck__unknown_shop__not_found() -> None:
    world = World()

    with pytest.raises(ShopNotFoundError):
        await recheck_capabilities(world.checks, auth(), uuid.uuid4(), now=NOW, correlation_id=None)


async def test_recheck__within_the_cooldown__refused_with_the_wait() -> None:
    world = World()
    await recheck_capabilities(world.checks, auth(), SHOP, now=NOW, correlation_id=None)
    world.probe.calls.clear()

    with pytest.raises(RecheckTooSoonError) as too_soon:
        await recheck_capabilities(
            world.checks, auth(), SHOP, now=NOW + timedelta(seconds=10), correlation_id=None
        )

    assert too_soon.value.retry_after_seconds == 20
    assert world.probe.calls == []
    assert timedelta(seconds=30) == RECHECK_COOLDOWN


async def test_recheck__after_the_cooldown__probes_again() -> None:
    world = World()
    await recheck_capabilities(world.checks, auth(), SHOP, now=NOW, correlation_id=None)

    await recheck_capabilities(
        world.checks, auth(), SHOP, now=NOW + RECHECK_COOLDOWN, correlation_id=None
    )

    assert len(world.store.saved) == 2


async def test_recheck__disconnected__refused_without_any_etsy_call() -> None:
    world = World()
    await world.vault.disconnect(SHOP, purge_after=NOW + timedelta(days=30))

    with pytest.raises(ShopNotConnectedError):
        await recheck_capabilities(world.checks, auth(), SHOP, now=NOW, correlation_id=None)

    assert world.probe.calls == []


# --- Codex review of #49 -----------------------------------------------------------------------


async def test_recheck__two_at_once__one_probes_and_the_other_is_told_to_wait() -> None:
    """The cooldown is reserved before probing, so two presses cannot both call Etsy."""
    world = World()
    gate = anyio.Event()
    original = world.probe.get_shop

    async def slow_get_shop(shop_id: int, *, urgent: bool = False) -> MarketplaceShop:
        await gate.wait()
        return await original(shop_id, urgent=urgent)

    world.probe.get_shop = slow_get_shop  # type: ignore[method-assign]
    outcomes: list[str] = []

    async def press() -> None:
        try:
            await recheck_capabilities(world.checks, auth(), SHOP, now=NOW, correlation_id=None)
            outcomes.append("probed")
        except RecheckTooSoonError:
            outcomes.append("wait")
            gate.set()

    with anyio.fail_after(5):  # both probing at once would wait on the gate forever
        async with anyio.create_task_group() as group:
            group.start_soon(press)
            group.start_soon(press)

    assert sorted(outcomes) == ["probed", "wait"]
    assert len(world.store.saved) == 1


async def test_recheck__fractional_wait__rounds_retry_after_up() -> None:
    world = World()
    await recheck_capabilities(world.checks, auth(), SHOP, now=NOW, correlation_id=None)

    with pytest.raises(RecheckTooSoonError) as too_soon:
        await recheck_capabilities(
            world.checks,
            auth(),
            SHOP,
            now=NOW + timedelta(seconds=10, milliseconds=400),
            correlation_id=None,
        )

    assert too_soon.value.retry_after_seconds == 20


async def test_probe__superseded_by_a_disconnect_or_reconnect__leaves_no_trace() -> None:
    """Codex review of #49: when the store refuses the results (the connection generation moved
    on), nothing else happens either: no audit, no success time, no results answered."""
    world = World()
    world.store.accept = False

    with pytest.raises(ProbeSupersededError):
        await world.probe_now()

    assert world.audit.records == []
    assert world.store.audits == []
    assert world.integration.last_successful_call_at is None
    assert world.store.rows == {}


def test_probe_superseded__is_a_not_connected_answer() -> None:
    assert issubclass(ProbeSupersededError, ShopNotConnectedError)


async def test_probe__overtaken_by_a_newer_probe__leaves_no_trace() -> None:
    """Codex review of #49: the store refuses results older than what it holds."""
    world = World()
    await world.store.save(
        ORG,
        SHOP,
        [
            CapabilityResult(
                ShopCapability.SHOP_DETAILS,
                CapabilityState.AVAILABLE,
                "read_ok",
                NOW + timedelta(minutes=1),
            )
        ],
        generation=world.integration.generation,
    )

    with pytest.raises(ProbeSupersededError):
        await world.probe_now()

    assert len(world.store.audits) == 0


async def test_recheck__connection_replaced_before_the_claim__no_etsy_call() -> None:
    """Codex review of #49: the cooldown claim is bound to the connection that was loaded."""
    world = World()
    world.store.superseded_on_reserve = True

    with pytest.raises(ProbeSupersededError):
        await recheck_capabilities(world.checks, auth(), SHOP, now=NOW, correlation_id=None)

    assert world.probe.calls == []
    assert world.store.reserved_generations == []


async def test_probe__every_etsy_call_uses_tokens_pinned_to_the_probed_connection() -> None:
    """Codex review of #49: never the token of whichever connection holds the Etsy shop now."""
    world = World()

    await world.probe_now()

    assert world.probe.token_sources == [("pinned", SHOP, world.integration.generation)] * 3
