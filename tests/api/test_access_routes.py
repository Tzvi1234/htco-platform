"""Member and shop-access management endpoints (slice 2.6, AC-2.6.1 to AC-2.6.4)."""

import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from htco_api.app import AppOptions, create_app
from htco_api.auth import Authenticator
from htco_application.access.authentication import AuthenticatedUser, Membership
from htco_application.access.management import Inviter as InviterPort
from htco_domain.identity import Role
from htco_platform.jwt import JwtVerifier
from tests.access_fakes import (
    ADMIN,
    INVITE_LINK,
    M_ADMIN,
    M_ELSEWHERE,
    M_OWNER,
    M_SUPPORT,
    NEWCOMER,
    NOW,
    ORG,
    OWNER,
    SHOP,
    SUPPORT,
    Inviter,
    Store,
)
from tests.auth_tokens import AUDIENCE, ISSUER, FakeJwks, SigningKey, claims
from tests.idempotency_fakes import MemoryIdempotencyStore, fresh_idempotency_key
from tests.log_capture import LogCapture

pytestmark = pytest.mark.anyio

KEY = SigningKey("k1")
PROBLEMS = "https://ht-co.app/problems/"
BASE = f"/v1/organizations/{ORG}"


class DatabaseMemberships:
    """Reads the caller's memberships from the same store the endpoints change (AC-2.6.3)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def memberships_of(self, user: AuthenticatedUser) -> Sequence[Membership]:
        return [
            Membership(
                row.organization_id,
                row.role,
                frozenset(shop for (member, shop) in self.store.grants if member == row.id),
            )
            for row in self.store.members.values()
            if row.user_id == user.user_id
        ]


def client_for(
    store: Store,
    user: uuid.UUID,
    *,
    aal: str = "aal2",
    inviter: InviterPort | None = None,
    idempotency: MemoryIdempotencyStore | None = None,
) -> httpx.AsyncClient:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(
            authenticator=Authenticator(verifier, DatabaseMemberships(store)),
            access=store,
            inviter=inviter if inviter is not None else Inviter(),
            idempotency=idempotency if idempotency is not None else MemoryIdempotencyStore(),
            clock=lambda: NOW,
        )
    )
    token = KEY.sign(claims(sub=str(user), aal=aal))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {token}"},
        event_hooks={"request": [fresh_idempotency_key]},
    )


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
async def owner(store: Store) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(store, OWNER) as client:
        yield client


@pytest.fixture
async def admin(store: Store) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(store, ADMIN) as client:
        yield client


async def test_list_members(admin: httpx.AsyncClient) -> None:
    response = await admin.get(f"{BASE}/members")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [(item["id"], item["role"]) for item in items] == [
        (str(M_OWNER), "owner"),
        (str(M_ADMIN), "admin"),
        (str(M_SUPPORT), "support"),
    ]
    assert items[0] == {
        "id": str(M_OWNER),
        "user_id": str(OWNER),
        "display_name": "Owner",
        "role": "owner",
        "shop_ids": [],
    }


async def test_invite__201_and_the_new_member(admin: httpx.AsyncClient, store: Store) -> None:
    response = await admin.post(
        f"{BASE}/invitations", json={"email": "new@example.test", "role": "support"}
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["user_id"], body["role"]) == (str(NEWCOMER), "support")
    [record] = store.audit
    assert record.correlation_id == response.headers["x-request-id"]


@pytest.mark.parametrize(
    "body",
    [{"email": "not-an-email", "role": "viewer"}, {"email": "a@b.c", "role": "superuser"}, {}],
    ids=["bad-email", "bad-role", "empty"],
)
async def test_invite__invalid_body__422(owner: httpx.AsyncClient, body: dict[str, str]) -> None:
    response = await owner.post(f"{BASE}/invitations", json=body)
    assert response.status_code == 422
    assert response.json()["type"] == PROBLEMS + "validation-error"


async def test_invite__existing_member__409(store: Store) -> None:
    async with client_for(store, OWNER, inviter=Inviter(SUPPORT)) as client:
        response = await client.post(
            f"{BASE}/invitations", json={"email": "s@x.test", "role": "viewer"}
        )
    assert response.status_code == 409
    assert response.json()["type"] == PROBLEMS + "member-exists"


async def test_invite__registered_elsewhere__409(store: Store) -> None:
    async with client_for(store, OWNER, inviter=Inviter(registered=True)) as client:
        response = await client.post(
            f"{BASE}/invitations", json={"email": "s@x.test", "role": "viewer"}
        )
    assert response.status_code == 409
    assert response.json()["type"] == PROBLEMS + "user-already-registered"


async def test_invite__returns_the_one_time_link_uncached_and_unlogged(
    logs: LogCapture, owner: httpx.AsyncClient
) -> None:
    """No email provider yet: the Owner or Admin copies the link and sends it themselves."""
    response = await owner.post(
        f"{BASE}/invitations", json={"email": "new@example.test", "role": "admin"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["invite_link"] == INVITE_LINK
    assert (body["user_id"], body["role"], body["shop_ids"]) == (str(NEWCOMER), "admin", [])
    assert set(body) == {"id", "user_id", "display_name", "role", "shop_ids", "invite_link"}
    assert response.headers["cache-control"] == "no-store"
    assert INVITE_LINK not in logs.raw()
    assert "one-time" not in logs.raw()


async def test_invite__existing_account_added__no_link(store: Store) -> None:
    store.registered["known@example.test"] = NEWCOMER
    async with client_for(store, OWNER, inviter=Inviter(registered=True)) as client:
        response = await client.post(
            f"{BASE}/invitations", json={"email": "known@example.test", "role": "viewer"}
        )
    assert response.status_code == 201
    assert response.json()["invite_link"] is None
    assert response.json()["user_id"] == str(NEWCOMER)
    assert response.headers["cache-control"] == "no-store"


async def test_invite__same_idempotency_key__invites_once_and_never_stores_the_link(
    store: Store,
) -> None:
    """Architecture audit 6.1: a timed-out retry does not send a second invitation; its replay
    is the first answer without the link, which is shown once and never stored."""
    inviter, idempotency = Inviter(), MemoryIdempotencyStore()
    body = {"email": "new@example.test", "role": "viewer"}
    headers = {"Idempotency-Key": "invite-1"}
    async with client_for(store, OWNER, inviter=inviter, idempotency=idempotency) as client:
        first = await client.post(f"{BASE}/invitations", json=body, headers=headers)
        again = await client.post(f"{BASE}/invitations", json=body, headers=headers)

    assert [first.status_code, again.status_code] == [201, 201]
    assert first.json()["invite_link"] == INVITE_LINK
    assert again.json() == first.json() | {"invite_link": None}
    assert again.headers["idempotent-replayed"] == "true"
    assert again.headers["cache-control"] == "no-store"
    assert inviter.invited == ["new@example.test"]
    assert [r.action for r in store.audit] == ["membership.created"]
    assert INVITE_LINK not in repr(idempotency.rows)


async def test_invite__without_an_idempotency_key__400_and_nobody_invited(store: Store) -> None:
    inviter = Inviter()
    async with client_for(store, OWNER, inviter=inviter) as client:
        client.event_hooks["request"] = []
        response = await client.post(
            f"{BASE}/invitations", json={"email": "new@example.test", "role": "viewer"}
        )
    assert response.status_code == 400
    assert response.json()["type"] == PROBLEMS + "idempotency-key-required"
    assert inviter.invited == []


async def test_change_role_remove_grant_and_revoke(owner: httpx.AsyncClient, store: Store) -> None:
    changed = await owner.patch(f"{BASE}/members/{M_SUPPORT}", json={"role": "finance"})
    granted = await owner.put(f"{BASE}/members/{M_SUPPORT}/shops/{SHOP}")
    revoked = await owner.delete(f"{BASE}/members/{M_SUPPORT}/shops/{SHOP}")
    removed = await owner.delete(f"{BASE}/members/{M_SUPPORT}")

    assert changed.status_code == 200
    assert changed.json()["role"] == "finance"
    assert [granted.status_code, revoked.status_code, removed.status_code] == [204, 204, 204]
    assert [record.action for record in store.audit] == [
        "membership.role_changed",
        "shop_access.granted",
        "shop_access.revoked",
        "membership.revoked",
    ]


@pytest.mark.parametrize("role", ["operations", "finance", "support", "developer", "viewer"])
async def test_every_other_role__403_forbidden(store: Store, role: str) -> None:
    """AC-2.6.1."""
    store.update_role(M_SUPPORT, Role(role))
    async with client_for(store, SUPPORT) as client:
        responses = [
            await client.get(f"{BASE}/members"),
            await client.post(f"{BASE}/invitations", json={"email": "n@x.test", "role": "viewer"}),
            await client.patch(f"{BASE}/members/{M_ADMIN}", json={"role": "viewer"}),
            await client.delete(f"{BASE}/members/{M_ADMIN}"),
            await client.put(f"{BASE}/members/{M_ADMIN}/shops/{SHOP}"),
            await client.delete(f"{BASE}/members/{M_ADMIN}/shops/{SHOP}"),
        ]
    assert {r.status_code for r in responses} == {403}
    assert {r.json()["type"] for r in responses} == {PROBLEMS + "forbidden"}
    assert store.audit == []


async def test_owner_with_aal1__403_mfa_required(store: Store) -> None:
    """AC-2.6.1."""
    async with client_for(store, OWNER, aal="aal1") as client:
        response = await client.patch(f"{BASE}/members/{M_SUPPORT}", json={"role": "viewer"})
    assert response.status_code == 403
    assert response.json()["type"] == PROBLEMS + "mfa-required"


async def test_admin__cannot_grant_owner_or_remove_the_owner(admin: httpx.AsyncClient) -> None:
    """AC-2.6.2."""
    promote = await admin.patch(f"{BASE}/members/{M_SUPPORT}", json={"role": "owner"})
    invite = await admin.post(f"{BASE}/invitations", json={"email": "n@x.test", "role": "owner"})
    remove = await admin.delete(f"{BASE}/members/{M_OWNER}")
    assert [promote.status_code, invite.status_code, remove.status_code] == [403, 403, 403]


async def test_revoked_member__is_refused_on_the_next_request_with_the_same_token(
    store: Store, owner: httpx.AsyncClient
) -> None:
    """AC-2.6.3."""
    async with client_for(store, ADMIN) as admin_client:
        assert (await admin_client.get(f"{BASE}/members")).status_code == 200
        assert (await owner.delete(f"{BASE}/members/{M_ADMIN}")).status_code == 204
        after = await admin_client.get(f"{BASE}/members")
    assert after.status_code == 403
    assert after.json()["type"] == PROBLEMS + "no-membership"


@pytest.mark.parametrize(
    "path",
    [
        f"{BASE}/members/{M_ELSEWHERE}",
        f"{BASE}/members/{uuid.UUID(int=7)}",
    ],
    ids=["other-tenant", "unknown"],
)
async def test_membership_outside_the_organization__404(
    owner: httpx.AsyncClient, path: str
) -> None:
    response = await owner.patch(path, json={"role": "viewer"})
    assert response.status_code == 404


async def test_shop_outside_the_organization__404(owner: httpx.AsyncClient) -> None:
    response = await owner.put(f"{BASE}/members/{M_SUPPORT}/shops/{uuid.UUID(int=5)}")
    assert response.status_code == 404


async def test_revoking_a_grant_that_does_not_exist__404(owner: httpx.AsyncClient) -> None:
    response = await owner.delete(f"{BASE}/members/{M_SUPPORT}/shops/{SHOP}")
    assert response.status_code == 404


async def test_another_organization__403(owner: httpx.AsyncClient) -> None:
    response = await owner.get(f"/v1/organizations/{uuid.UUID(int=3)}/members")
    assert response.status_code == 403


async def test_without_an_inviter__invitations_are_503(store: Store) -> None:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(
        AppOptions(authenticator=Authenticator(verifier, DatabaseMemberships(store)), access=store)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims(sub=str(OWNER), aal='aal2'))}"},
    ) as client:
        invite = await client.post(
            f"{BASE}/invitations", json={"email": "n@x.test", "role": "viewer"}
        )
        members = await client.get(f"{BASE}/members")
    assert invite.status_code == 503
    assert invite.json()["type"] == PROBLEMS + "invitations-unavailable"
    assert members.status_code == 200


async def test_without_an_access_store__503(store: Store) -> None:
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=FakeJwks(KEY))
    app = create_app(AppOptions(authenticator=Authenticator(verifier, DatabaseMemberships(store))))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers={"Authorization": f"Bearer {KEY.sign(claims(sub=str(OWNER), aal='aal2'))}"},
    ) as client:
        response = await client.get(f"{BASE}/members")
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        (f"{BASE}/members/{M_SUPPORT}", "DELETE, PATCH"),
        (f"{BASE}/members/{M_SUPPORT}/shops/{SHOP}", "DELETE, PUT"),
        (f"{BASE}/members", "GET"),
    ],
)
async def test_wrong_method__405_lists_every_method_of_the_path(
    owner: httpx.AsyncClient, path: str, allowed: str
) -> None:
    response = await owner.request("OPTIONS", path)
    assert response.status_code == 405
    assert response.headers["allow"] == allowed
