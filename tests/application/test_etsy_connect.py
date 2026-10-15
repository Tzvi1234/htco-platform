"""The Etsy OAuth connect flow (slice 4.3): start, then complete with the callback's code and state.

The state and PKCE verifier are kept server-side for ten minutes, bound to the owner's web
session, and used once. A callback with a missing, reused, expired or foreign state is rejected
and audited (AC-4.3.1). Connecting creates or updates one shop per Etsy shop and organization
(AC-4.3.2), grants only the connecting owner (AC-4.3.3) and records the granted scopes (AC-4.3.4).
"""

import base64
import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from htco_application.access.authentication import AuthContext, Membership
from htco_application.etsy_connect import (
    AUTHORIZATION_LIFETIME,
    ETSY_CONNECT_SCOPES,
    ConnectedShop,
    ConnectNotAllowedError,
    EtsyAccountHasNoShopError,
    EtsyAuthorizationRefusedError,
    EtsyConnect,
    EtsyShopConnectedElsewhereError,
    EtsyUnreachableError,
    StateRejectedError,
    TokenGrant,
    complete_connection,
    start_connection,
)
from htco_domain.actors import ActorKind
from htco_domain.identity import AssuranceLevel, Role
from tests.etsy_fakes import (
    ETSY_SHOP,
    ETSY_USER,
    GOOD_CODE,
    NOW,
    REDIRECT_URI,
    AuditLog,
    AuthorizationServer,
    Connector,
    Directory,
    Pending,
    etsy_shop,
)

pytestmark = pytest.mark.anyio

ORG = uuid.UUID("a0000000-0000-0000-0000-000000000000")
OTHER_ORG = uuid.UUID("b0000000-0000-0000-0000-000000000000")
OWNER = uuid.UUID("11111111-0000-0000-0000-000000000001")
STRANGER = uuid.UUID("11111111-0000-0000-0000-000000000002")
SESSION = uuid.UUID("5e550000-0000-0000-0000-000000000001")
OTHER_SESSION = uuid.UUID("5e550000-0000-0000-0000-000000000002")
REQUESTED = frozenset({"shops_r", "transactions_r", "listings_r"})


def owner(user: uuid.UUID = OWNER, *, org: uuid.UUID = ORG, role: Role = Role.OWNER) -> AuthContext:
    return AuthContext(
        user_id=user,
        assurance=AssuranceLevel.AAL2,
        session_id=None,
        memberships=(Membership(org, role, frozenset()),),
        reauthenticated_at=NOW,
    )


class World:
    def __init__(self) -> None:
        self.server = AuthorizationServer()
        self.directory = Directory()
        self.pending = Pending()
        self.audit = AuditLog()
        self.connector = Connector(self.audit)
        self.etsy = EtsyConnect(
            authorization_server=self.server,
            directory=self.directory,
            pending=self.pending,
            connector=self.connector,
            audit=self.audit,
            redirect_uri=REDIRECT_URI,
        )

    async def start(
        self, *, user: uuid.UUID = OWNER, session: uuid.UUID = SESSION, at: datetime = NOW
    ) -> str:
        url = await start_connection(
            self.etsy, organization_id=ORG, user_id=user, session_id=session, now=at
        )
        [state] = parse_qs(urlsplit(url).query)["state"]
        return state

    async def complete(
        self,
        state: str | None,
        *,
        auth: AuthContext | None = None,
        session: uuid.UUID = SESSION,
        code: str = GOOD_CODE,
        at: datetime = NOW,
    ) -> ConnectedShop:
        return await complete_connection(
            self.etsy,
            auth or owner(),
            session_id=session,
            code=code,
            state=state,
            now=at,
            correlation_id="req-1",
        )


@pytest.fixture
def world() -> World:
    return World()


# --- Start -------------------------------------------------------------------------------------


async def test_start__authorization_url_has_pkce_s256_state_scopes_and_the_exact_redirect(
    world: World,
) -> None:
    url = await start_connection(
        world.etsy, organization_id=ORG, user_id=OWNER, session_id=SESSION, now=NOW
    )

    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://www.etsy.com/oauth/connect?")
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert set(query["scope"][0].split(" ")) == REQUESTED == ETSY_CONNECT_SCOPES
    assert query["code_challenge_method"] == ["S256"]
    [pending] = world.pending.rows.values()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(pending.code_verifier.encode()).digest())
    assert query["code_challenge"] == [challenge.rstrip(b"=").decode()]


async def test_start__state_has_at_least_128_bits_and_is_stored_only_as_a_hash(
    world: World,
) -> None:
    state = await world.start()

    [pending] = world.pending.rows.values()
    assert len(base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))) >= 16
    assert pending.state_hash == hashlib.sha256(state.encode()).digest()
    assert state.encode() not in pending.state_hash
    assert 43 <= len(pending.code_verifier) <= 128  # RFC 7636 §4.1
    assert state not in repr(pending)
    assert pending.code_verifier not in repr(pending)


async def test_start__pending_authorization_is_bound_to_the_session_for_ten_minutes(
    world: World,
) -> None:
    await world.start()

    [pending] = world.pending.rows.values()
    assert timedelta(minutes=10) == AUTHORIZATION_LIFETIME
    assert (pending.organization_id, pending.user_id, pending.session_id) == (ORG, OWNER, SESSION)
    assert (pending.created_at, pending.expires_at) == (NOW, NOW + timedelta(minutes=10))
    assert pending.scopes == REQUESTED
    assert pending.redirect_uri == REDIRECT_URI


async def test_start__every_connection_gets_its_own_state_and_verifier(world: World) -> None:
    first, second = await world.start(), await world.start()

    verifiers = {p.code_verifier for p in world.pending.rows.values()}
    assert first != second
    assert len(verifiers) == 2


# --- Complete: the happy path ------------------------------------------------------------------


@pytest.mark.ac("AC-4.3.3")
async def test_complete__exchanges_the_code_with_the_verifier_and_connects_the_shop(
    world: World,
) -> None:
    state = await world.start()
    verifier = next(iter(world.pending.rows.values())).code_verifier

    connected = await world.complete(state)

    assert world.server.exchanged == [(GOOD_CODE, verifier, REDIRECT_URI)]
    [(asked_user, asked_token)] = world.directory.asked
    assert asked_user == ETSY_USER
    assert asked_token.startswith(f"{ETSY_USER}.")
    [request] = world.connector.requests
    assert (request.organization_id, request.owner_user_id) == (ORG, OWNER)
    assert (request.etsy_shop_id, request.etsy_user_id) == (ETSY_SHOP, ETSY_USER)
    assert request.shop_name == "FakeCraftsShop"
    assert request.tokens.access_expires_at == NOW + timedelta(hours=1)
    assert request.tokens.refresh_expires_at == NOW + timedelta(days=90)
    assert request.correlation_id == "req-1"
    assert world.connector.grants == [(connected.shop.id, OWNER)]
    assert world.pending.rows == {}


async def test_complete__audits_the_connection_as_the_owner(world: World) -> None:
    await world.complete(await world.start())

    [record] = world.audit.records
    assert record.action == "shop.connected"
    assert (record.actor.kind, record.actor.id) == (ActorKind.USER, str(OWNER))
    assert record.organization_id == ORG
    assert record.after is not None
    assert record.after["scopes"] == sorted(REQUESTED)
    assert record.correlation_id == "req-1"
    assert "fake-access" not in repr(record.after)


@pytest.mark.ac("AC-4.3.4")
async def test_complete__records_the_scopes_etsy_granted_when_it_names_them(world: World) -> None:
    world.server.scopes = frozenset({"shops_r", "transactions_r"})

    await world.complete(await world.start())

    assert world.connector.requests[0].scopes == frozenset({"shops_r", "transactions_r"})


@pytest.mark.ac("AC-4.3.2")
async def test_complete__connecting_the_same_shop_twice__one_shop(world: World) -> None:
    first = await world.complete(await world.start())
    second = await world.complete(await world.start())

    assert len(world.connector.shops) == 1
    assert first.shop.id == second.shop.id
    assert second.reconnected is True
    assert [r.action for r in world.audit.records] == ["shop.connected", "shop.reconnected"]


# --- Complete: rejected states (AC-4.3.1) ------------------------------------------------------


def _rejections(world: World) -> list[str]:
    return [
        str(r.after["reason"])
        for r in world.audit.records
        if r.action == "shop.connect_rejected" and r.after is not None
    ]


@pytest.mark.ac("AC-4.3.1")
@pytest.mark.parametrize("state", [None, "", "   "])
async def test_complete__missing_state__rejected_audited_and_no_exchange(
    world: World, state: str | None
) -> None:
    with pytest.raises(StateRejectedError) as raised:
        await world.complete(state)

    assert raised.value.reason == "missing"
    assert world.server.exchanged == []
    assert _rejections(world) == ["missing"]
    [record] = world.audit.records
    assert record.organization_id == ORG
    assert (record.actor.kind, record.actor.id) == (ActorKind.USER, str(OWNER))


@pytest.mark.ac("AC-4.3.1")
async def test_complete__unknown_state__rejected_and_audited(world: World) -> None:
    with pytest.raises(StateRejectedError) as raised:
        await world.complete("never-issued-state-value")

    assert raised.value.reason == "unknown"
    assert _rejections(world) == ["unknown"]


@pytest.mark.ac("AC-4.3.1")
async def test_complete__reused_state__rejected_and_audited(world: World) -> None:
    state = await world.start()
    await world.complete(state)

    with pytest.raises(StateRejectedError):
        await world.complete(state)

    assert len(world.server.exchanged) == 1
    assert _rejections(world) == ["unknown"]  # a used state no longer exists


@pytest.mark.ac("AC-4.3.1")
async def test_complete__expired_state__rejected_audited_and_consumed(world: World) -> None:
    state = await world.start()

    with pytest.raises(StateRejectedError) as raised:
        await world.complete(state, at=NOW + timedelta(minutes=10))

    assert raised.value.reason == "expired"
    assert world.server.exchanged == []
    assert world.pending.rows == {}
    assert _rejections(world) == ["expired"]


async def test_complete__one_second_before_expiry__accepted(world: World) -> None:
    state = await world.start()

    await world.complete(state, at=NOW + timedelta(minutes=10) - timedelta(seconds=1))

    assert len(world.connector.requests) == 1


@pytest.mark.ac("AC-4.3.1")
async def test_complete__state_of_another_session__rejected_and_audited(world: World) -> None:
    state = await world.start()

    with pytest.raises(StateRejectedError) as raised:
        await world.complete(state, session=OTHER_SESSION)

    assert raised.value.reason == "foreign"
    assert world.server.exchanged == []
    assert _rejections(world) == ["foreign"]


@pytest.mark.ac("AC-4.3.1")
async def test_complete__state_of_another_user__rejected_and_audited(world: World) -> None:
    state = await world.start()

    with pytest.raises(StateRejectedError) as raised:
        await world.complete(state, auth=owner(STRANGER))

    assert raised.value.reason == "foreign"
    assert world.server.exchanged == []


async def test_complete__rejection_of_a_user_in_several_organizations__audited_in_each_owned(
    world: World,
) -> None:
    both = replace(
        owner(),
        memberships=(
            Membership(ORG, Role.OWNER, frozenset()),
            Membership(OTHER_ORG, Role.VIEWER, frozenset()),
        ),
    )

    with pytest.raises(StateRejectedError):
        await world.complete(None, auth=both)

    assert [r.organization_id for r in world.audit.records] == [ORG]


# --- Complete: the caller and Etsy -------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "assurance"),
    [(Role.ADMIN, AssuranceLevel.AAL2), (Role.OWNER, AssuranceLevel.AAL1)],
)
async def test_complete__caller_no_longer_owner_with_mfa__refused_before_the_exchange(
    world: World, role: Role, assurance: AssuranceLevel
) -> None:
    state = await world.start()
    caller = replace(owner(role=role), assurance=assurance)

    with pytest.raises(ConnectNotAllowedError):
        await world.complete(state, auth=caller)

    assert world.server.exchanged == []


async def test_complete__step_up_lapsed_since_the_start__still_completes(world: World) -> None:
    """The start needed a fresh step-up; the callback follows within the state's ten minutes."""
    state = await world.start()
    lapsed = replace(owner(), reauthenticated_at=NOW - timedelta(minutes=15))

    await world.complete(state, auth=lapsed, at=NOW + timedelta(minutes=5))

    assert len(world.connector.requests) == 1


async def test_complete__etsy_refuses_the_code__authorization_refused_and_audited(
    world: World,
) -> None:
    state = await world.start()

    with pytest.raises(EtsyAuthorizationRefusedError):
        await world.complete(state, code="bad-code")

    assert world.connector.requests == []
    assert _rejections(world) == ["code_refused"]


async def test_complete__etsy_unreachable__unreachable(world: World) -> None:
    world.server.exchange_outcome = "unavailable"
    state = await world.start()

    with pytest.raises(EtsyUnreachableError):
        await world.complete(state)

    assert world.connector.requests == []


async def test_complete__etsy_account_without_a_shop__no_shop(world: World) -> None:
    world.directory.shops = {}
    state = await world.start()

    with pytest.raises(EtsyAccountHasNoShopError):
        await world.complete(state)

    assert world.connector.requests == []


async def test_complete__shop_connected_in_another_organization__refused(world: World) -> None:
    await world.complete(await world.start())
    other = World()
    other.connector.integrations = {
        k: replace(v, organization_id=OTHER_ORG) for k, v in world.connector.integrations.items()
    }

    with pytest.raises(EtsyShopConnectedElsewhereError):
        await other.complete(await other.start())


def test_token_grant__user_id_is_the_access_token_prefix() -> None:
    grant = TokenGrant(
        access_token=f"{ETSY_USER}.abc",
        refresh_token=f"{ETSY_USER}.def",
        expires_in=timedelta(hours=1),
    )

    assert grant.user_id == ETSY_USER
    assert f"{ETSY_USER}.abc" not in repr(grant)


@pytest.mark.parametrize(
    "token", ["abc", ".abc", "0.abc", "-5.abc", "12a.abc", chr(0xFF11) + chr(0xFF12) + ".abc"]
)
def test_token_grant__access_token_without_a_numeric_prefix__refused(token: str) -> None:
    with pytest.raises(ValueError, match="user ID"):
        TokenGrant(access_token=token, refresh_token="1.x", expires_in=timedelta(hours=1))  # noqa: S106 - test value


def test_token_grant__non_positive_lifetime__refused() -> None:
    with pytest.raises(ValueError, match="lifetime"):
        TokenGrant(access_token="1.a", refresh_token="1.b", expires_in=timedelta(0))  # noqa: S106 - test value


async def test_complete__directory_shop_name_is_kept(world: World) -> None:
    world.directory.shops = {ETSY_USER: etsy_shop(name="שלום Shop")}

    connected = await world.complete(await world.start())

    assert connected.shop.name == "שלום Shop"
