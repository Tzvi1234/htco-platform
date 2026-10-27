"""The Action Gateway's channel adapters (slice 5.13, `execution-engine.md` §3).

- `EtsyApiChannel`: real. It reads through `MarketplacePort`, the guarded Etsy adapter that
  sends GET requests only. Every write recipe is refused before anything leaves HTCO: Etsy writes
  stay disabled until the owner-triggered slices of Phases 6 and 7, which deliver them through
  the outbox with one `Deliverer` per write action.
- `InternalChannel`: runs an application use case by recipe name.
- `BrowserChannel` (the Protocol) and `FakeBrowserChannel`, which replays recorded recipe
  outcomes, including a security challenge (the real controller is Phase 8A).
- `HumanChannel` and `MemoryHumanTasks`: the hand-off contract. The gateway creates one task per
  execution and resumes the execution when the task is completed or cancelled (the Postgres task
  store is Phase 7).
"""

import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from htco_application.execution.contracts import ChannelBinding, ChannelOutcome
from htco_application.execution.gateway import ActionRequest, Channel
from htco_application.jobs import JsonValue
from htco_application.marketplace import (
    MarketplaceMoney,
    MarketplaceNotFoundError,
    MarketplacePermissionError,
    MarketplaceShop,
    MarketplaceWriteRefusedError,
    OrderReceipt,
    Page,
    Receipt,
    ShopReview,
)
from htco_application.order_receipts import for_automation, receipt_to_json
from htco_domain.actors import ActorKind

SECURITY_CHALLENGE = "security_challenge"
HUMAN_CHANNEL = "human_channel"


# --- API ----------------------------------------------------------------------------------------


class EtsyReads(Protocol):
    """The read operations of `MarketplacePort` the API channel uses."""

    async def get_shop(self, shop_id: int, *, urgent: bool = False) -> MarketplaceShop: ...

    async def list_receipts(  # noqa: PLR0913 - the port's filters
        self,
        shop_id: int,
        *,
        min_last_modified: datetime | None = None,
        max_last_modified: datetime | None = None,
        limit: int = 25,
        offset: int = 0,
        urgent: bool = False,
    ) -> Page[Receipt]: ...

    async def get_receipt(self, shop_id: int, receipt_id: int) -> Receipt: ...

    async def list_order_receipts(  # noqa: PLR0913 - the port's filters
        self,
        shop_id: int,
        *,
        min_created: datetime | None,
        min_last_modified: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        urgent: bool = False,
        was_paid: bool | None = None,
        was_shipped: bool | None = None,
    ) -> Page[OrderReceipt]: ...

    async def get_order_receipt(self, shop_id: int, receipt_id: int) -> OrderReceipt: ...

    async def list_reviews(
        self,
        shop_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
        min_created: datetime | None = None,
    ) -> Page[ShopReview]: ...


class EtsyShopIds(Protocol):
    def etsy_shop_id(self, shop_id: uuid.UUID) -> int | None: ...


def _money(money: MarketplaceMoney) -> dict[str, JsonValue]:
    return {"amount": money.amount, "divisor": money.divisor, "currency_code": money.currency_code}


def _receipt(receipt: Receipt) -> dict[str, JsonValue]:
    """Order facts only: `Receipt` carries no buyer details, and neither does this."""
    return {
        "receipt_id": receipt.receipt_id,
        "status": receipt.status,
        "is_paid": receipt.is_paid,
        "is_shipped": receipt.is_shipped,
        "created_at": receipt.created_at.isoformat(),
        "updated_at": receipt.updated_at.isoformat(),
        "grand_total": _money(receipt.grand_total),
        "lines": [
            {
                "transaction_id": line.transaction_id,
                "listing_id": line.listing_id,
                "sku": line.sku,
                "quantity": line.quantity,
                "unit_price": _money(line.unit_price),
            }
            for line in receipt.lines
        ],
    }


def _review(review: ShopReview) -> dict[str, JsonValue]:
    """Identifiers, stars and time: an R0 output may be stored, so never the buyer's words.
    A review without its transaction (Etsy's null) leaves the key out."""
    mapped: dict[str, JsonValue] = {
        "listing_id": review.listing_id,
        "rating": review.rating,
        "created_at": review.created_at.isoformat(),
    }
    if review.transaction_id is not None:
        mapped["transaction_id"] = review.transaction_id
    return mapped


type _Recipe = Callable[[int, ActionRequest], Awaitable[ChannelOutcome]]


class EtsyApiChannel:
    channel = Channel.API

    def __init__(self, etsy: EtsyReads, shops: EtsyShopIds) -> None:
        self._etsy = etsy
        self._shops = shops
        # The recipes this channel runs (AUD-06, #147): the gateway refuses at startup a binding
        # whose recipe is not here, unless it is disabled.
        self._recipes: Mapping[str, _Recipe] = {
            "get_shop": self._get_shop,
            "list_receipts": self._list_receipts,
            "list_reviews": self._list_reviews,
        }

    @property
    def recipes(self) -> frozenset[str]:
        return frozenset(self._recipes)

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome:
        recipe = None if binding.recipe is None else self._recipes.get(binding.recipe)
        if recipe is None:
            raise MarketplaceWriteRefusedError(
                f"{request.action.name}: no recipe {binding.recipe} in the API channel"
            )
        etsy_shop = None if request.shop_id is None else self._shops.etsy_shop_id(request.shop_id)
        if etsy_shop is None:
            raise MarketplaceNotFoundError("the shop has no Etsy connection", status=404)
        return await recipe(etsy_shop, request)

    async def _get_shop(self, etsy_shop: int, request: ActionRequest) -> ChannelOutcome:  # noqa: ARG002
        shop = await self._etsy.get_shop(etsy_shop)
        return ChannelOutcome(
            output={"shop_id": shop.shop_id, "name": shop.name, "currency": shop.currency_code}
        )

    async def _list_reviews(self, etsy_shop: int, request: ActionRequest) -> ChannelOutcome:
        return ChannelOutcome(output=await self._reviews(etsy_shop, request.input))

    async def _list_receipts(self, etsy_shop: int, request: ActionRequest) -> ChannelOutcome:
        if request.input.get("order_history") is True:
            if request.actor.kind is not ActorKind.AUTOMATION:
                raise MarketplacePermissionError(
                    "the order history form is for automations", status=403
                )
            # The buyer's name and address are Etsy Order Intake's alone, and tracking codes
            # Intake's and Tracking Sync's (ADR-0018, AUD-06): every other automation gets the
            # same receipts without them.
            return ChannelOutcome(
                output=await self._order_history(etsy_shop, request.input, request.actor.id)
            )
        return ChannelOutcome(output=await self._receipts(etsy_shop, request.input))

    async def _order_history(
        self, etsy_shop: int, values: Mapping[str, JsonValue], automation_id: str
    ) -> dict[str, JsonValue]:
        """Receipts as the order history stores them (`order_receipts`), in the form this
        automation may receive (`for_automation`): an R1 output the gateway returns and never
        stores."""

        def shape(receipt: OrderReceipt) -> dict[str, JsonValue]:
            return receipt_to_json(for_automation(receipt, automation_id))

        receipt_id = values.get("receipt_id")
        if isinstance(receipt_id, int):
            one = await self._etsy.get_order_receipt(etsy_shop, receipt_id)
            return {"receipts": [shape(one)], "count": 1}
        since = values.get("min_last_modified")
        page = await self._etsy.list_order_receipts(
            etsy_shop,
            min_created=None,
            min_last_modified=None
            if not isinstance(since, int)
            else datetime.fromtimestamp(since, UTC),
            limit=int(str(values.get("limit", 100))),
            offset=int(str(values.get("offset", 0))),
            was_paid=_flag(values.get("was_paid")),
            was_shipped=_flag(values.get("was_shipped")),
        )
        return {"receipts": [shape(r) for r in page.items], "count": page.total_count}

    async def _receipts(
        self, etsy_shop: int, values: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        receipt_id = values.get("receipt_id")
        if isinstance(receipt_id, int):  # one receipt, for example from a webhook (5.3, 5.4)
            one = await self._etsy.get_receipt(etsy_shop, receipt_id)
            return {"receipts": [_receipt(one)], "count": 1}
        since = values.get("min_last_modified")
        page = await self._etsy.list_receipts(
            etsy_shop,
            min_last_modified=None
            if not isinstance(since, int)
            else datetime.fromtimestamp(since, UTC),
            limit=int(str(values.get("limit", 25))),
            offset=int(str(values.get("offset", 0))),
        )
        return {"receipts": [_receipt(r) for r in page.items], "count": page.total_count}

    async def _reviews(
        self, etsy_shop: int, values: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        since = values.get("min_created")
        page = await self._etsy.list_reviews(
            etsy_shop,
            limit=int(str(values.get("limit", 100))),
            offset=int(str(values.get("offset", 0))),
            min_created=None if not isinstance(since, int) else datetime.fromtimestamp(since, UTC),
        )
        return {"reviews": [_review(r) for r in page.items], "count": page.total_count}

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool:  # noqa: ARG002
        return False  # no write runs through this channel yet, so nothing is re-read


def _flag(value: JsonValue) -> bool | None:
    return value if isinstance(value, bool) else None


# --- Internal -----------------------------------------------------------------------------------

UseCase = Callable[[ActionRequest], Awaitable[Mapping[str, JsonValue]]]


class InternalChannel:
    channel = Channel.INTERNAL

    @property
    def recipes(self) -> frozenset[str]:
        return frozenset(self._use_cases)

    def __init__(self, use_cases: Mapping[str, UseCase]) -> None:
        self._use_cases = use_cases

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome:
        use_case = self._use_cases.get(binding.recipe or "")
        if use_case is None:
            raise LookupError(f"no internal use case {binding.recipe!r}")
        return ChannelOutcome(output=dict(await use_case(request)))

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool:  # noqa: ARG002
        return True  # a use case commits in our own database: its result is the state


# --- Browser ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    problems: tuple[str, ...] = ()


class BrowserChannel(Protocol):
    channel: Channel

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome: ...

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool: ...

    async def preflight(self, environment_id: uuid.UUID) -> PreflightReport: ...


class RecipeKind(StrEnum):
    OK = "ok"
    CHALLENGE = "challenge"  # CAPTCHA, passkey, block page: stop and hand to a person
    ERROR = "error"


@dataclass(frozen=True)
class RecipeOutcome:
    kind: RecipeKind
    output: Mapping[str, JsonValue] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def ok(cls, output: Mapping[str, JsonValue]) -> RecipeOutcome:
        return cls(RecipeKind.OK, output)

    @classmethod
    def challenge(cls) -> RecipeOutcome:
        return cls(RecipeKind.CHALLENGE)

    @classmethod
    def failed(cls, code: str) -> RecipeOutcome:
        return cls(RecipeKind.ERROR, error=code)


class BrowserRecipeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"browser recipe failed: {code}")
        self.code = code


@dataclass
class FakeBrowserChannel:
    """Replays recorded recipe outcomes; never starts a browser."""

    recorded: Mapping[str, RecipeOutcome]
    channel: Channel = Channel.BROWSER
    calls: list[tuple[ActionRequest, ChannelBinding]] = field(default_factory=list)

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome:
        self.calls.append((request, binding))
        outcome = self.recorded[binding.recipe or ""]
        if outcome.kind is RecipeKind.CHALLENGE:
            return ChannelOutcome(handed_to_human=True, handoff_reason=SECURITY_CHALLENGE)
        if outcome.kind is RecipeKind.ERROR:
            raise BrowserRecipeError(outcome.error or "unknown")
        return ChannelOutcome(output=dict(outcome.output))

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool:  # noqa: ARG002
        return True

    async def preflight(self, environment_id: uuid.UUID) -> PreflightReport:  # noqa: ARG002
        return PreflightReport(ok=True)


# --- Human --------------------------------------------------------------------------------------


class HumanChannel:
    """A binding a person carries out: the gateway hands the action off as a task."""

    channel = Channel.HUMAN

    async def execute(self, request: ActionRequest, binding: ChannelBinding) -> ChannelOutcome:  # noqa: ARG002
        return ChannelOutcome(handed_to_human=True, handoff_reason=HUMAN_CHANNEL)

    async def verify(self, request: ActionRequest, outcome: ChannelOutcome) -> bool:  # noqa: ARG002
        return True  # the person's result is checked against the output schema


class HumanTaskState(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class HumanTask:
    id: uuid.UUID
    execution_id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID | None
    action: str
    reason: str
    state: HumanTaskState = HumanTaskState.OPEN
    output: Mapping[str, JsonValue] | None = None
    note: str | None = None


class HumanTasks(Protocol):
    def hand_off(
        self, execution_id: uuid.UUID, request: ActionRequest, *, reason: str
    ) -> uuid.UUID:
        """One task per execution: a second hand-off returns the same task."""
        ...

    def task_of(self, execution_id: uuid.UUID) -> HumanTask | None: ...


@dataclass
class MemoryHumanTasks:
    tasks: dict[uuid.UUID, HumanTask] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def hand_off(
        self, execution_id: uuid.UUID, request: ActionRequest, *, reason: str
    ) -> uuid.UUID:
        with self._lock:
            existing = self._of(execution_id)
            if existing is not None:
                return existing.id
            task = HumanTask(
                id=uuid.uuid7(), execution_id=execution_id,
                organization_id=request.organization_id, shop_id=request.shop_id,
                action=request.action.name, reason=reason,
            )  # fmt: skip
            self.tasks[task.id] = task
            return task.id

    def task_of(self, execution_id: uuid.UUID) -> HumanTask | None:
        with self._lock:
            return self._of(execution_id)

    def complete(self, task_id: uuid.UUID, output: Mapping[str, JsonValue]) -> None:
        with self._lock:
            task = self.tasks[task_id]
            task.state, task.output = HumanTaskState.COMPLETED, dict(output)

    def cancel(self, task_id: uuid.UUID, note: str) -> None:
        with self._lock:
            task = self.tasks[task_id]
            task.state, task.note = HumanTaskState.CANCELLED, note

    def _of(self, execution_id: uuid.UUID) -> HumanTask | None:
        return next((t for t in self.tasks.values() if t.execution_id == execution_id), None)
