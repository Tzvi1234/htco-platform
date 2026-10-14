"""Listing the caller's shops (slice 2.4 part 2, AC-2.4.2 and AC-2.4.3)."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from htco_application.access.authentication import AuthContext, Membership, NoMembershipError
from htco_application.shops import (
    MAX_PAGE_SIZE,
    OrganizationRequiredError,
    ShopNotFoundError,
    ShopQuery,
    ShopSummary,
    list_shops,
)
from htco_domain.identity import AssuranceLevel, Role

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
USER = uuid.UUID("11111111-0000-0000-0000-000000000001")
ORG_A = uuid.UUID("a0000000-0000-0000-0000-000000000000")
ORG_B = uuid.UUID("b0000000-0000-0000-0000-000000000000")
CLAIMS = {"sub": str(USER), "role": "authenticated"}


def shop(n: int, org: uuid.UUID = ORG_A) -> ShopSummary:
    return ShopSummary(
        id=uuid.UUID(int=n),
        organization_id=org,
        name=f"Shop {n}",
        marketplace="etsy",
        status="pending",
    )


class Shops:
    """An in-memory shop store that behaves like the repository: ordered by ID, filtered."""

    def __init__(self, *shops: ShopSummary) -> None:
        self.shops = sorted(shops, key=lambda s: s.id)
        self.queries: list[tuple[Mapping[str, Any], ShopQuery]] = []

    async def list_shops(
        self, claims: Mapping[str, Any], query: ShopQuery
    ) -> Sequence[ShopSummary]:
        self.queries.append((claims, query))
        found = [
            s
            for s in self.shops
            if s.organization_id == query.organization_id
            and (query.only_shops is None or s.id in query.only_shops)
            and (query.shop_id is None or s.id == query.shop_id)
            and (query.after is None or s.id > query.after)
        ]
        return found[: query.limit]


def auth(*memberships: Membership) -> AuthContext:
    return AuthContext(
        user_id=USER,
        assurance=AssuranceLevel.AAL1,
        session_id=None,
        memberships=memberships,
        claims=CLAIMS,
    )


OWNER_A = auth(Membership(ORG_A, Role.OWNER, frozenset()))
SUPPORT_A = auth(Membership(ORG_A, Role.SUPPORT, frozenset({uuid.UUID(int=2)})))


async def test_owner__sees_every_shop_of_the_organization() -> None:
    store = Shops(shop(1), shop(2), shop(3), shop(9, ORG_B))
    page = await list_shops(OWNER_A, store, now=NOW)

    assert [s.id.int for s in page.items] == [1, 2, 3]
    assert page.next_cursor is None
    claims, query = store.queries[0]
    assert claims == CLAIMS
    assert query.only_shops is None


async def test_support__sees_only_granted_shops_and_the_query_says_so() -> None:
    store = Shops(shop(1), shop(2), shop(3))
    page = await list_shops(SUPPORT_A, store, now=NOW)

    assert [s.id.int for s in page.items] == [2]
    assert store.queries[0][1].only_shops == frozenset({uuid.UUID(int=2)})


async def test_shop_filter__a_granted_shop__returns_it() -> None:
    page = await list_shops(SUPPORT_A, Shops(shop(1), shop(2)), shop_id=uuid.UUID(int=2), now=NOW)
    assert [s.id.int for s in page.items] == [2]


@pytest.mark.parametrize("target", [1, 9, 77], ids=["not-granted", "other-tenant", "unknown"])
async def test_shop_filter__a_shop_the_user_cannot_see__is_not_found(target: int) -> None:
    """AC-2.4.2: 404 whatever the reason, so other tenants' shops cannot be probed."""
    store = Shops(shop(1), shop(2), shop(9, ORG_B))
    with pytest.raises(ShopNotFoundError):
        await list_shops(SUPPORT_A, store, shop_id=uuid.UUID(int=target), now=NOW)


async def test_owner__shop_filter_naming_another_tenants_shop__is_not_found() -> None:
    store = Shops(shop(1), shop(9, ORG_B))
    with pytest.raises(ShopNotFoundError):
        await list_shops(OWNER_A, store, shop_id=uuid.UUID(int=9), now=NOW)


async def test_pages__are_stable_and_do_not_overlap() -> None:
    """AC-2.4.3."""
    store = Shops(*(shop(n) for n in range(1, 8)))
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        page = await list_shops(OWNER_A, store, limit=3, after=cursor, now=NOW)
        seen.extend(s.id.int for s in page.items)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert seen == [1, 2, 3, 4, 5, 6, 7]
    assert pages == 3


async def test_page__exactly_full__has_no_next_cursor() -> None:
    page = await list_shops(OWNER_A, Shops(shop(1), shop(2)), limit=2, now=NOW)
    assert [s.id.int for s in page.items] == [1, 2]
    assert page.next_cursor is None


async def test_page__asks_the_store_for_one_more_than_the_limit() -> None:
    store = Shops(shop(1), shop(2), shop(3))
    page = await list_shops(OWNER_A, store, limit=2, now=NOW)
    assert store.queries[0][1].limit == 3
    assert page.next_cursor == uuid.UUID(int=2)


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1])
async def test_limit__outside_the_allowed_range__is_refused(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await list_shops(OWNER_A, Shops(), limit=limit, now=NOW)


async def test_several_organizations__without_one_named__is_refused() -> None:
    both = auth(
        Membership(ORG_A, Role.OWNER, frozenset()), Membership(ORG_B, Role.VIEWER, frozenset())
    )
    with pytest.raises(OrganizationRequiredError):
        await list_shops(both, Shops(), now=NOW)

    page = await list_shops(both, Shops(shop(9, ORG_B)), organization_id=ORG_B, now=NOW)
    assert [s.id.int for s in page.items] == []  # a viewer with no granted shops


async def test_named_organization__not_a_member__is_refused() -> None:
    with pytest.raises(NoMembershipError):
        await list_shops(OWNER_A, Shops(), organization_id=ORG_B, now=NOW)
