"""The "New product quotes" board (slice 7.16), read only.

`GET /v1/shops/{shop_id}/quote-requests` returns one card per product waiting for a supplier,
with its quotes side by side, sorted by total landed cost. Amounts are decimal strings, computed
on the server (AC-7.16.2); a quote past its validity has `choosable: false`. For the Owner and
Admins of the shop's organization. Identifiers, money and terms only: no buyer data, no mail.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from htco_api.auth import CurrentAuth
from htco_api.identity import problem_responses
from htco_api.policy_guard import FORBIDDEN
from htco_api.problems import NOT_FOUND, ProblemError, ProblemType, ValidationProblem
from htco_application.kernel import ShopOwners
from htco_application.quote_board import ProductQuotes, QuoteBoardReader, read_quote_board
from htco_application.shops import ShopAccessDeniedError, ShopNotFoundError

QUOTES_UNAVAILABLE = ProblemType(
    "quotes-unavailable", "Quotes not available", 503, "Quotes cannot be read on this server."
)


@dataclass(frozen=True)
class QuoteBoardServices:
    reader: QuoteBoardReader
    shops: ShopOwners


class DayRangeView(BaseModel):
    min: int
    max: int


class QuoteCardView(BaseModel):
    id: uuid.UUID
    supplier: str
    currency: str
    # Decimal strings, never floats: "7.40".
    unit_price: str
    shipping_cost: str
    total: str
    shipping_method: str
    processing_days: DayRangeView
    shipping_days: DayRangeView
    valid_until: date | None
    received_at: datetime
    choosable: bool


class ProductQuotesView(BaseModel):
    request_id: uuid.UUID
    listing_id: int
    variant: str
    state: str  # waiting_for_quotes or quotes_received
    requested_at: datetime
    quotes: list[QuoteCardView]


class QuoteBoardView(BaseModel):
    shop_id: uuid.UUID
    items: list[ProductQuotesView]


def _money(value: object) -> str:
    return f"{value:.2f}"


def _view(product: ProductQuotes) -> ProductQuotesView:
    return ProductQuotesView(
        request_id=product.request_id,
        listing_id=product.listing_id,
        variant=product.variant,
        state=product.state,
        requested_at=product.requested_at,
        quotes=[
            QuoteCardView(
                id=q.id,
                supplier=q.supplier,
                currency=q.currency,
                unit_price=_money(q.unit_price),
                shipping_cost=_money(q.shipping_cost),
                total=_money(q.total),
                shipping_method=q.shipping_method,
                processing_days=DayRangeView(min=q.processing_days[0], max=q.processing_days[1]),
                shipping_days=DayRangeView(min=q.shipping_days[0], max=q.shipping_days[1]),
                valid_until=q.valid_until,
                received_at=q.received_at,
                choosable=q.choosable,
            )
            for q in product.quotes
        ],
    )


def _services(request: Request) -> QuoteBoardServices:
    found = getattr(request.app.state, "quote_board", None)
    if not isinstance(found, QuoteBoardServices):
        raise ProblemError(QUOTES_UNAVAILABLE)
    return found


_Services = Annotated[QuoteBoardServices, Depends(_services)]


def quotes_router(*, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(prefix="/v1/shops/{shop_id}", tags=["quotes"])

    @router.get(
        "/quote-requests",
        summary="Products waiting for a supplier, with their quotes side by side",
        responses=problem_responses(
            {
                403: "Only the Owner and Admins see supplier quotes (forbidden)",
                404: "No such shop for the caller",
                422: ("Invalid shop ID", ValidationProblem),
                503: "Quotes cannot be read on this server (quotes-unavailable)",
            }
        ),
    )
    async def list_quote_requests(
        shop_id: uuid.UUID, response: Response, auth: CurrentAuth, services: _Services
    ) -> QuoteBoardView:
        try:
            board = await anyio.to_thread.run_sync(
                lambda: read_quote_board(
                    auth, services.reader, services.shops, shop_id, now=clock()
                )
            )
        except ShopNotFoundError:
            raise ProblemError(NOT_FOUND) from None
        except ShopAccessDeniedError:
            raise ProblemError(FORBIDDEN) from None
        response.headers["Cache-Control"] = "no-store"
        return QuoteBoardView(shop_id=shop_id, items=[_view(p) for p in board])

    return router
