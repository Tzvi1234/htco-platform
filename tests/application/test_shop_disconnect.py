"""Disconnecting a shop (slice 4.7): the Owner, with MFA and a recent step-up, deletes the tokens;
the shop is marked disconnected with a purge date 30 days ahead, audited, and no Etsy call is made
for it afterwards (AC-4.7.1, proven with the real adapter over a strict HTTP mock).

Etsy documents no token revocation endpoint (authentication docs and the OpenAPI spec, checked
2026-10-16), so nothing is sent to Etsy: the audit record says the revocation was not offered.
"""

import uuid
from datetime import timedelta

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from htco_application.access.authentication import AuthContext, Membership
from htco_application.access.policy import Outcome
from htco_application.etsy_tokens import VaultAccessTokenProvider
from htco_application.marketplace import MarketplaceAuthError
from htco_application.shop_capabilities import (
    CapabilityChecks,
    ShopNotConnectedError,
    recheck_capabilities,
)
from htco_application.shop_integrations import (
    DisconnectNotAllowedError,
    EtsyConnection,
    OAuthTokens,
    ShopIntegration,
    TokenVaultError,
    disconnect_shop,
)
from htco_application.shops import ShopNotFoundError
from htco_domain.identity import AssuranceLevel, Role
from htco_domain.integrations import IntegrationStatus
from htco_integrations.etsy import EtsyAdapter, EtsyCredentials
from htco_platform.http import new_async_http_client
from tests.etsy_fakes import (
    ETSY_SHOP,
    NOW,
    AuditLog,
    AuthorizationServer,
    Capabilities,
    Lock,
    Vault,
)

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
SHOP = uuid.UUID("a3000000-0000-0000-0000-000000000001")
USER = uuid.UUID("a1000000-0000-0000-0000-000000000001")
SCOPES = frozenset({"shops_r", "transactions_r", "listings_r"})


def connected_vault() -> Vault:
    vault = Vault()
    vault.add(
        EtsyConnection(
            organization_id=ORG,
            shop_id=SHOP,
            etsy_shop_id=ETSY_SHOP,
            etsy_user_id=12345678,
            scopes=SCOPES,
            tokens=OAuthTokens(
                access_token="12345678.fake-access-token",  # noqa: S106 - fake
                refresh_token="12345678.fake-refresh-token",  # noqa: S106 - fake
                access_expires_at=NOW + timedelta(hours=1),
                refresh_expires_at=NOW + timedelta(days=90),
            ),
        )
    )
    return vault


def owner(
    *,
    role: Role = Role.OWNER,
    assurance: AssuranceLevel = AssuranceLevel.AAL2,
    stepped_up: bool = True,
    org: uuid.UUID = ORG,
) -> AuthContext:
    return AuthContext(
        user_id=USER,
        assurance=assurance,
        session_id=None,
        memberships=(Membership(org, role, frozenset()),),
        reauthenticated_at=NOW - timedelta(minutes=2) if stepped_up else None,
    )


async def test_disconnect__deletes_the_tokens_sets_the_purge_date_and_audits() -> None:
    vault = connected_vault()

    after = await disconnect_shop(vault, owner(), SHOP, now=NOW, correlation_id="req-9")

    assert after.status is IntegrationStatus.DISCONNECTED
    assert after.tokens is None
    assert after.purge_after == NOW + timedelta(days=30)
    [record] = vault.audit
    assert record.action == "shop.disconnected"
    assert record.entity_type == "shop"
    assert record.entity_id == str(SHOP)
    assert record.organization_id == ORG
    assert record.correlation_id == "req-9"
    assert record.before == {"status": "connected", "scopes": sorted(SCOPES)}
    assert record.after == {
        "status": "disconnected",
        "purge_after": (NOW + timedelta(days=30)).isoformat(),
        "etsy_revocation": "not_offered_by_etsy",
    }
    assert "fake-access-token" not in repr(record)


async def test_disconnect__twice__second_is_a_no_op_without_a_second_audit() -> None:
    vault = connected_vault()
    await disconnect_shop(vault, owner(), SHOP, now=NOW, correlation_id=None)

    again = await disconnect_shop(
        vault, owner(), SHOP, now=NOW + timedelta(minutes=1), correlation_id=None
    )

    assert again.status is IntegrationStatus.DISCONNECTED
    assert again.purge_after == NOW + timedelta(days=30)
    assert len(vault.audit) == 1


@pytest.mark.parametrize(
    ("caller", "outcome"),
    [
        (owner(role=Role.ADMIN), Outcome.DENY),
        (owner(role=Role.OPERATIONS), Outcome.DENY),
        (owner(assurance=AssuranceLevel.AAL1), Outcome.MFA_REQUIRED),
        (owner(stepped_up=False), Outcome.STEP_UP_REQUIRED),
    ],
)
async def test_disconnect__not_owner_with_mfa_and_step_up__refused_and_nothing_changes(
    caller: AuthContext, outcome: Outcome
) -> None:
    vault = connected_vault()

    with pytest.raises(DisconnectNotAllowedError) as refused:
        await disconnect_shop(vault, caller, SHOP, now=NOW, correlation_id=None)

    assert refused.value.outcome is outcome
    assert vault.rows[SHOP].status is IntegrationStatus.CONNECTED
    assert vault.audit == []


async def test_disconnect__shop_of_another_organization__not_found() -> None:
    vault = connected_vault()
    other = uuid.UUID("b0000000-0000-0000-0000-000000000000")

    with pytest.raises(ShopNotFoundError):
        await disconnect_shop(vault, owner(org=other), SHOP, now=NOW, correlation_id=None)

    assert vault.rows[SHOP].status is IntegrationStatus.CONNECTED


async def test_disconnect__unknown_shop__not_found() -> None:
    with pytest.raises(ShopNotFoundError):
        await disconnect_shop(
            connected_vault(), owner(), uuid.uuid4(), now=NOW, correlation_id=None
        )


# --- AC-4.7.1: after disconnect, no Etsy call for that shop -----------------------------------


class StrictEtsy:
    """Records every request that reaches 'Etsy'; any request at all fails the test."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        pytest.fail(f"an Etsy call was made after disconnect: {request.method} {request.url}")


@pytest.mark.ac("AC-4.7.1")
async def test_after_disconnect__no_etsy_call_is_made_for_the_shop() -> None:
    vault = connected_vault()
    etsy = StrictEtsy()
    server = AuthorizationServer()
    client = new_async_http_client(
        tracer_provider=TracerProvider(), transport=httpx.MockTransport(etsy)
    )
    adapter = EtsyAdapter(
        client,
        credentials=EtsyCredentials(keystring="fakekeystring", shared_secret="fake"),  # noqa: S106
        access_tokens=VaultAccessTokenProvider(vault, server, Lock(), clock=lambda: NOW),
    )
    checks = CapabilityChecks(
        vault=vault, marketplace=adapter, store=Capabilities(), audit=AuditLog()
    )

    await disconnect_shop(vault, owner(), SHOP, now=NOW, correlation_id=None)

    async with client:
        with pytest.raises(MarketplaceAuthError):
            await adapter.get_shop(ETSY_SHOP, urgent=True)
        with pytest.raises(MarketplaceAuthError):
            await adapter.list_receipts(ETSY_SHOP)
        with pytest.raises(MarketplaceAuthError):
            await adapter.check_receipt_addresses(ETSY_SHOP)
        with pytest.raises(ShopNotConnectedError):
            await recheck_capabilities(checks, owner(), SHOP, now=NOW, correlation_id=None)

    assert etsy.requests == []
    assert server.refreshed == []


class LostKeyVault(Vault):
    """The stored tokens cannot be opened any more (the wrapping key was lost)."""

    async def load(self, shop_id: uuid.UUID) -> ShopIntegration | None:
        raise TokenVaultError.unreadable(shop_id)


async def test_disconnect__tokens_that_cannot_be_opened__still_disconnects() -> None:
    """Codex review of #49: disconnecting is the recovery after a lost key, so it reads only the
    connection's metadata and never opens the tokens."""
    vault = LostKeyVault()
    vault.rows = connected_vault().rows

    after = await disconnect_shop(vault, owner(), SHOP, now=NOW, correlation_id=None)

    assert after.status is IntegrationStatus.DISCONNECTED
    assert [record.action for record in vault.audit] == ["shop.disconnected"]
