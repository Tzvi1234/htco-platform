"""`GET /v1/me` and `GET /v1/shops` (slice 2.4 part 2).

Both need a signed-in caller (`CurrentAuth`). `/v1/shops` lists only the shops the caller may
see; naming a shop or an organization they cannot see answers 404, the same as one that does not
exist (AC-2.4.2). Pages are cursor-based: the cursor is opaque to clients (AC-2.4.3).
"""

import base64
import binascii
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from htco_api.auth import CurrentAuth
from htco_api.problems import (
    NOT_FOUND,
    NOT_READY,
    PROBLEM_MEDIA_TYPE,
    Problem,
    ProblemError,
    ProblemType,
    ValidationProblem,
)
from htco_application.access.authentication import NoMembershipError
from htco_application.shops import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    OrganizationRequiredError,
    ShopAccessDeniedError,
    ShopNotFoundError,
    ShopReader,
    list_shops,
)
from htco_domain.identity import Role

ORGANIZATION_REQUIRED = ProblemType(
    "organization-required",
    "Organization required",
    400,
    "You belong to several organizations: name one with `organization_id`.",
)
FORBIDDEN = ProblemType("forbidden", "Forbidden", 403, "Your role does not allow this.")
_CURSOR_MAX_LENGTH = 64


class MembershipView(BaseModel):
    organization_id: uuid.UUID
    role: Role


class Me(BaseModel):
    user_id: uuid.UUID
    assurance: Literal["aal1", "aal2"]
    memberships: list[MembershipView]


class ShopView(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    marketplace: str
    status: str


class ShopList(BaseModel):
    items: list[ShopView]
    # Pass it back as `cursor` for the next page; null on the last page.
    next_cursor: str | None


def _problem(description: str, model: type[Problem] = Problem) -> dict[str, Any]:
    return {"model": model, "content": {PROBLEM_MEDIA_TYPE: {}}, "description": description}


_AUTH_DESCRIPTIONS: dict[int, str | tuple[str, type[Problem]]] = {
    401: "Not signed in, or the access token was rejected",
    403: "Signed in, but not a member of any organization",
    503: "Sign-in or the database is unavailable",
}


def problem_responses(
    described: Mapping[int, str | tuple[str, type[Problem]]] | None = None,
) -> dict[int | str, dict[str, Any]]:
    """OpenAPI problem responses for authenticated routes: the sign-in ones plus `described`."""
    merged = {**_AUTH_DESCRIPTIONS, **(described or {})}
    return {
        status: _problem(*(text if isinstance(text, tuple) else (text,)))
        for status, text in merged.items()
    }


def encode_cursor(after: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(after.bytes).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> uuid.UUID:
    invalid = RequestValidationError(
        [{"loc": ("query", "cursor"), "msg": "Invalid cursor", "type": "value_error"}]
    )
    if len(cursor) > _CURSOR_MAX_LENGTH:
        raise invalid
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        return uuid.UUID(bytes=raw)
    except binascii.Error, ValueError:
        raise invalid from None


def _shop_reader(request: Request) -> ShopReader:
    shops = getattr(request.app.state, "shops", None)
    if shops is None:
        raise ProblemError(NOT_READY)
    reader: ShopReader = shops
    return reader


def identity_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["identity"], responses=problem_responses())

    @router.get("/me", summary="The signed-in user and their organizations")
    async def me(auth: CurrentAuth) -> Me:
        """Who the caller is, how strongly they signed in, and their role in each organization."""
        return Me(
            user_id=auth.user_id,
            assurance="aal2" if auth.assurance.value == "aal2" else "aal1",
            memberships=[
                MembershipView(organization_id=m.organization_id, role=m.role)
                for m in auth.memberships
            ],
        )

    @router.get(
        "/shops",
        summary="The shops the caller may see",
        responses={
            400: _problem("The caller belongs to several organizations and named none"),
            404: _problem("The named shop or organization does not exist for the caller"),
            422: _problem("Invalid filter, limit or cursor", ValidationProblem),
        },
    )
    async def shops(  # noqa: PLR0913, PLR0917 - FastAPI passes one parameter per query filter
        auth: CurrentAuth,
        reader: Annotated[ShopReader, Depends(_shop_reader)],
        organization_id: uuid.UUID | None = None,
        shop_id: uuid.UUID | None = None,
        cursor: Annotated[str | None, Query(max_length=_CURSOR_MAX_LENGTH * 4)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    ) -> ShopList:
        """Ordered by creation; Owner and Admin see every shop, other roles their granted shops."""
        after = decode_cursor(cursor) if cursor is not None else None
        try:
            page = await list_shops(
                auth,
                reader,
                now=clock(),
                organization_id=organization_id,
                shop_id=shop_id,
                after=after,
                limit=limit,
            )
        except OrganizationRequiredError:
            raise ProblemError(ORGANIZATION_REQUIRED) from None
        except NoMembershipError, ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except ShopAccessDeniedError:  # pragma: no cover - every role may view shops today
            raise ProblemError(FORBIDDEN) from None
        return ShopList(
            items=[ShopView(**vars(shop)) for shop in page.items],
            next_cursor=None if page.next_cursor is None else encode_cursor(page.next_cursor),
        )

    return router
