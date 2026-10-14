"""The caller's shops (slice 2.4 part 2): who sees which shop, one page at a time.

The policy engine decides the scope (every shop of the organization, or only the granted ones)
and the query carries that scope to the repository, which also runs under the caller's RLS: the
application filter and the database agree, and either alone would be enough. A shop the caller
cannot see does not exist for them: naming it returns "not found", never "forbidden", so other
tenants' shops cannot be probed (AC-2.4.2). Pages follow shop IDs, which are UUID v7 and so
ordered by creation; a page asks for one more row than it returns to know whether more exist.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, decide

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


class OrganizationRequiredError(Exception):
    """The caller belongs to several organizations and the request named none."""


class ShopNotFoundError(Exception):
    """The named shop does not exist for this caller."""


class ShopAccessDeniedError(Exception):
    """The caller's role may not view shops (no role is in this state today)."""


@dataclass(frozen=True)
class ShopSummary:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    marketplace: str
    status: str


@dataclass(frozen=True)
class ShopQuery:
    organization_id: uuid.UUID
    # None: every shop of the organization; otherwise only these.
    only_shops: frozenset[uuid.UUID] | None
    shop_id: uuid.UUID | None
    after: uuid.UUID | None
    limit: int


class ShopReader(Protocol):
    async def list_shops(
        self, claims: Mapping[str, Any], query: ShopQuery
    ) -> Sequence[ShopSummary]: ...


@dataclass(frozen=True)
class ShopPage:
    items: tuple[ShopSummary, ...]
    next_cursor: uuid.UUID | None


async def list_shops(  # noqa: PLR0913 - every argument is a separate filter of one query
    auth: AuthContext,
    shops: ShopReader,
    *,
    now: datetime,
    organization_id: uuid.UUID | None = None,
    shop_id: uuid.UUID | None = None,
    after: uuid.UUID | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> ShopPage:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    principal = auth.principal_for(organization)
    decision = decide(principal, Capability.VIEW_OVERVIEW, shop=None, now=now)
    if decision.outcome is not Outcome.ALLOW:
        raise ShopAccessDeniedError(decision.outcome.value)
    query = ShopQuery(
        organization_id=organization,
        only_shops=None if decision.scope is Scope.ORGANIZATION else principal.granted_shops,
        shop_id=shop_id,
        after=after,
        limit=limit + 1,
    )
    found = tuple(await shops.list_shops(auth.claims, query))
    if shop_id is not None and not found:
        raise ShopNotFoundError("no such shop")
    items = found[:limit]
    next_cursor = items[-1].id if len(found) > limit else None
    return ShopPage(items=items, next_cursor=next_cursor)
