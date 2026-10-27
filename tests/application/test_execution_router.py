"""The Execution Router and the channel adapters (slice 5.13, `execution-engine.md` §3 and §4
step 5): deterministic binding choice, the real API adapter on a fake marketplace, the Internal
adapter, the fake Browser adapter with its challenge outcome and the Human hand-off. AC-5.13.1
to AC-5.13.5."""

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from htco_application.execution.channels import (
    EtsyApiChannel,
    FakeBrowserChannel,
    HumanChannel,
    InternalChannel,
    MemoryHumanTasks,
    RecipeOutcome,
)
from htco_application.execution.contracts import ActionCatalog, ActionCatalogError, ChannelBinding
from htco_application.execution.gateway import (
    ActionOutcome,
    ActionRequest,
    ActionResult,
    Channel,
)
from htco_application.execution.guards import Guards, MemoryBreakers, MemoryRateLimiter, MemorySlots
from htco_application.execution.service import (
    ActionGatewayService,
    ChannelAdapter,
    GatewayParts,
)
from htco_application.kernel import JsonValue, Page
from htco_application.marketplace import (
    MarketplaceMoney,
    MarketplaceNotFoundError,
    MarketplacePermissionError,
    MarketplaceShop,
    MarketplaceWriteRefusedError,
    OrderReceipt,
    Receipt,
    ReceiptLine,
    ShopReview,
)
from htco_domain.actions import ActionState
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.browser import EnvironmentStatus
from tests.application.test_action_gateway import NOW, ORG, SHOP, USER, World, ask, office
from tests.execution_fakes import (
    SCHEMAS,
    Approvals,
    Capabilities,
    MemoryActionStore,
    Shops,
    real_catalog,
)

pytestmark = pytest.mark.anyio

EMPLOYEE = ActorRef(ActorKind.EMPLOYEE, "eitan")
ETSY_SHOP = 4242
FLAG_ON = NOW - timedelta(days=1)
EMPTY: dict[str, JsonValue] = {"receipts": [], "count": 0}


# --- fakes ------------------------------------------------------------------------------------


@dataclass
class Browser:
    """The browser readiness of shops: environment status, action flags and Etsy scope rows."""

    status: EnvironmentStatus | None = EnvironmentStatus.READY
    flag_at: datetime | None = FLAG_ON
    scopes: dict[tuple[uuid.UUID, str], datetime] = field(
        default_factory=lambda: {(SHOP, "etsy.receipts.read"): FLAG_ON + timedelta(hours=1)}
    )

    def environment_status(self, shop_id: uuid.UUID) -> EnvironmentStatus | None:  # noqa: ARG002
        return self.status

    def flag_enabled_at(self, action_name: str) -> datetime | None:  # noqa: ARG002
        return self.flag_at

    def scope_recorded_at(self, shop_id: uuid.UUID, scope_id: str) -> datetime | None:
        return self.scopes.get((shop_id, scope_id))


@dataclass
class FakeMarketplace:
    """`MarketplacePort` without the network: answers from fixtures, records every call."""

    calls: list[tuple[str, int, dict[str, Any]]] = field(default_factory=list)
    receipts: tuple[Receipt, ...] = ()
    order_receipts: tuple[OrderReceipt, ...] = ()
    reviews: tuple[ShopReview, ...] = ()
    fail: Exception | None = None

    async def get_shop(self, shop_id: int, *, urgent: bool = False) -> MarketplaceShop:
        self.calls.append(("get_shop", shop_id, {"urgent": urgent}))
        if self.fail is not None:
            raise self.fail
        return MarketplaceShop(
            shop_id=shop_id, name="LeatherTest", currency_code="USD", url=None,
            is_vacation=False, active_listing_count=3, sold_transaction_count=9,
            created_at=NOW, updated_at=NOW,
        )  # fmt: skip

    async def list_receipts(self, shop_id: int, **filters: Any) -> Page[Receipt]:
        self.calls.append(("list_receipts", shop_id, filters))
        if self.fail is not None:
            raise self.fail
        return Page(items=self.receipts, total_count=len(self.receipts), offset=0)

    async def get_receipt(self, shop_id: int, receipt_id: int) -> Receipt:
        self.calls.append(("get_receipt", shop_id, {"receipt_id": receipt_id}))
        if self.fail is not None:
            raise self.fail
        return receipt(receipt_id)

    # The order history form (slice 5.4): whole receipts, buyer data included.
    async def list_order_receipts(self, shop_id: int, **filters: Any) -> Page[OrderReceipt]:
        self.calls.append(("list_order_receipts", shop_id, filters))
        if self.fail is not None:
            raise self.fail
        return Page(items=self.order_receipts, total_count=len(self.order_receipts), offset=0)

    async def get_order_receipt(self, shop_id: int, receipt_id: int) -> OrderReceipt:
        self.calls.append(("get_order_receipt", shop_id, {"receipt_id": receipt_id}))
        if self.fail is not None:
            raise self.fail
        found = next((r for r in self.order_receipts if r.receipt_id == receipt_id), None)
        if found is None:
            raise MarketplaceNotFoundError("no such receipt", status=404)
        return found

    async def list_reviews(self, shop_id: int, **filters: Any) -> Page[ShopReview]:
        self.calls.append(("list_reviews", shop_id, filters))
        if self.fail is not None:
            raise self.fail
        return Page(items=self.reviews, total_count=len(self.reviews), offset=0)


@dataclass
class EtsyShops:
    ids: dict[uuid.UUID, int] = field(default_factory=lambda: {SHOP: ETSY_SHOP})

    def etsy_shop_id(self, shop_id: uuid.UUID) -> int | None:
        return self.ids.get(shop_id)


def receipt(receipt_id: int = 77) -> Receipt:
    money = MarketplaceMoney(amount=2500, divisor=100, currency_code="USD")
    return Receipt(
        receipt_id=receipt_id, status="paid", is_paid=True, is_shipped=False,
        created_at=NOW, updated_at=NOW, grand_total=money,
        lines=(ReceiptLine(transaction_id=5, listing_id=6, sku="W-1", quantity=1,
                           unit_price=money),),
    )  # fmt: skip


def receipts_action(*bindings: dict[str, Any]) -> ActionCatalog:
    """`etsy.receipts.read` (R1, schema verification) with the given bindings."""
    registry = office()
    document = {
        "tool": "etsy.receipts.read",
        "approval_policy": "R1",
        "verify": "schema",
        "timeout_seconds": 60,
        "input_schema": "etsy.receipts.read.input.json",
        "output_schema": "etsy.receipts.read.output.json",
        "rate_limit_key": "etsy:app",
        "bindings": list(bindings),
    }
    schemas = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in SCHEMAS.glob("*.json")}
    return ActionCatalog.from_documents(
        [document],
        tools=registry.tools,
        schemas=schemas,
        rate_limits=[{"key": "etsy:app", "per_second": 100, "per_day": 10_000}],
    )


API = {"channel": "api", "recipe": "list_receipts", "requires_capability": "receipts_read"}
INTERNAL = {"channel": "internal", "recipe": "receipts_from_store"}
BROWSER = {
    "channel": "browser",
    "recipe": "read_receipts",
    "browser_scope_id": "etsy.receipts.read",
}
BROWSER_USE = BROWSER | {"deterministic": False}


class Office:
    def __init__(
        self,
        catalog: ActionCatalog,
        *adapters: ChannelAdapter,
        browser: Browser | None = None,
        capabilities: set[tuple[uuid.UUID, str]] | None = None,
        guards: Guards | None = None,
    ) -> None:
        self.registry = office()
        self.store = MemoryActionStore()
        self.human = MemoryHumanTasks()
        world = World()
        self.gateway = ActionGatewayService(
            GatewayParts(
                catalog=catalog,
                registry=lambda: self.registry,
                store=self.store,
                principals=world.principals,
                shops=Shops({SHOP: ORG}),
                approvals=Approvals(),
                adapters={adapter.channel: adapter for adapter in adapters},
                capabilities=Capabilities(
                    {(SHOP, "receipts_read")} if capabilities is None else capabilities
                ),
                clock=lambda: NOW,
                browser=browser or Browser(),
                human=self.human,
                guards=guards,
            )
        )


def recorded_browser(outcome: RecipeOutcome | None = None) -> FakeBrowserChannel:
    return FakeBrowserChannel({"read_receipts": outcome or RecipeOutcome.ok(EMPTY)})


def internal() -> InternalChannel:
    async def receipts_from_store(request: ActionRequest) -> dict[str, Any]:  # noqa: ARG001
        return dict(EMPTY)

    return InternalChannel({"receipts_from_store": receipts_from_store})


def shape(result: ActionResult) -> tuple[Any, ...]:
    return (result.outcome, result.verified, result.attempts, result.output, result.error_code)


# --- AC-5.13.1: one contract, every channel ---------------------------------------------------


@pytest.mark.ac("AC-5.13.1")
async def test_one_action__same_result_audit_and_event_through_api_internal_and_browser() -> None:
    catalog = receipts_action(API, INTERNAL, BROWSER)
    runs: dict[Channel, ChannelAdapter] = {
        Channel.API: EtsyApiChannel(FakeMarketplace(), EtsyShops()),
        Channel.INTERNAL: internal(),
        Channel.BROWSER: recorded_browser(),
    }
    seen = {}
    for channel, adapter in runs.items():
        office_ = Office(catalog, adapter)  # only this channel is deployed
        result = await office_.gateway.submit(ask())
        [step] = office_.store.only().steps[-1:]
        seen[channel] = (result, step)

    assert {r.channel for r, _ in seen.values()} == set(runs)
    shapes = [shape(r) for r, _ in seen.values()]
    assert all(s == shapes[0] for s in shapes)
    assert next(iter(seen.values()))[0].outcome is ActionOutcome.SUCCEEDED
    audits = {(s.audit.action, s.audit.entity_type) for _, s in seen.values() if s.audit}
    assert audits == {("action.succeeded", "action_execution")}  # R1: audited on every channel
    events = {tuple(sorted(s.event.payload)) for _, s in seen.values() if s.event}
    assert len(events) == 1


async def test_route__bindings_in_order__the_first_that_can_run() -> None:
    marketplace = FakeMarketplace()
    browser = recorded_browser()
    office_ = Office(receipts_action(API, INTERNAL, BROWSER), EtsyApiChannel(marketplace,
                     EtsyShops()), internal(), browser)  # fmt: skip

    result = await office_.gateway.submit(ask())

    assert result.channel is Channel.API
    assert browser.calls == []


async def test_route__the_channel_hint_is_a_preference_among_the_allowed() -> None:
    office_ = Office(receipts_action(API, INTERNAL), EtsyApiChannel(FakeMarketplace(),
                     EtsyShops()), internal())  # fmt: skip

    result = await office_.gateway.submit(ask(channel_hint=Channel.INTERNAL))

    assert result.channel is Channel.INTERNAL


async def test_route__nothing_deployed__no_channel() -> None:
    office_ = Office(receipts_action(API))

    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "no_channel")


# --- AC-5.13.2: automations get deterministic bindings only ----------------------------------


@pytest.mark.ac("AC-5.13.2")
async def test_automation__never_routed_to_browser_use__scope_missing() -> None:
    browser = recorded_browser()
    office_ = Office(receipts_action(BROWSER_USE), browser)

    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "scope_missing")
    assert browser.calls == []


@pytest.mark.ac("AC-5.13.2")
async def test_automation__takes_the_next_deterministic_binding() -> None:
    browser = recorded_browser()
    office_ = Office(receipts_action(BROWSER_USE, INTERNAL), browser, internal())

    result = await office_.gateway.submit(ask())

    assert result.channel is Channel.INTERNAL
    assert browser.calls == []


@pytest.mark.ac("AC-5.13.2")
async def test_employee__may_use_browser_use() -> None:
    browser = recorded_browser()
    office_ = Office(receipts_action(BROWSER_USE), browser)

    result = await office_.gateway.submit(ask(actor=EMPLOYEE))

    assert result.channel is Channel.BROWSER
    assert len(browser.calls) == 1


@pytest.mark.parametrize(
    ("binding", "problem"),
    [
        ({"channel": "api", "recipe": "x", "deterministic": False}, "only a browser binding"),
        ({"channel": "browser", "recipe": "x"}, "browser_scope_id"),
    ],
)
def test_catalog__browser_rules(binding: dict[str, Any], problem: str) -> None:
    with pytest.raises(ActionCatalogError, match=problem):
        receipts_action(binding)


# --- AC-5.13.3: a browser binding needs a ready environment, its flag and its scope -----------


@pytest.mark.ac("AC-5.13.3")
@pytest.mark.parametrize(
    ("browser", "code"),
    [
        *[
            (Browser(status=status), "environment_not_ready")
            for status in EnvironmentStatus
            if status is not EnvironmentStatus.READY
        ],
        (Browser(status=None), "environment_not_ready"),
        (Browser(flag_at=None), "flag_off"),
        (Browser(scopes={}), "scope_missing"),
        (
            Browser(scopes={(SHOP, "etsy.receipts.read"): FLAG_ON - timedelta(seconds=1)}),
            "scope_missing",
        ),
    ],
)
async def test_browser__never_chosen_unless_ready_flagged_and_scoped(
    browser: Browser, code: str
) -> None:
    channel = recorded_browser()
    office_ = Office(receipts_action(BROWSER), channel, browser=browser)

    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, code)
    assert channel.calls == []


@pytest.mark.ac("AC-5.13.3")
async def test_browser__ready_flagged_and_scoped__chosen() -> None:
    channel = recorded_browser()
    office_ = Office(receipts_action(BROWSER), channel)

    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.channel) == (ActionOutcome.SUCCEEDED, Channel.BROWSER)


# --- AC-5.13.4: a security challenge stops and hands off -------------------------------------


@pytest.mark.ac("AC-5.13.4")
async def test_challenge__handed_to_human_one_task_and_no_retry() -> None:
    channel = recorded_browser(RecipeOutcome.challenge())
    office_ = Office(receipts_action(BROWSER), channel)

    first = await office_.gateway.submit(ask())
    again = await office_.gateway.submit(ask())  # resubmitted while the task is open

    assert first.outcome is ActionOutcome.HANDED_TO_HUMAN
    assert again.outcome is ActionOutcome.HANDED_TO_HUMAN
    assert len(channel.calls) == 1
    [task] = office_.human.tasks.values()
    assert task.reason == "security_challenge"
    assert task.execution_id == first.execution_id
    assert office_.store.only().state is ActionState.HANDED_TO_HUMAN


@pytest.mark.ac("AC-5.13.4")
async def test_handed_to_human__resumes_once_the_task_is_completed() -> None:
    channel = recorded_browser(RecipeOutcome.challenge())
    office_ = Office(receipts_action(BROWSER), channel)
    first = await office_.gateway.submit(ask())
    [task_id] = office_.human.tasks

    office_.human.complete(task_id, {"receipts": [], "count": 0})
    done = await office_.gateway.submit(ask())
    replay = await office_.gateway.submit(ask())

    assert (done.outcome, done.channel) == (ActionOutcome.SUCCEEDED, Channel.BROWSER)
    assert done.execution_id == first.execution_id
    assert done.verified  # the person's result is checked against the output schema
    assert replay == done
    assert len(channel.calls) == 1


async def test_handed_to_human__an_invalid_result_from_the_person__failed() -> None:
    office_ = Office(receipts_action(BROWSER), recorded_browser(RecipeOutcome.challenge()))
    await office_.gateway.submit(ask())
    [task_id] = office_.human.tasks

    office_.human.complete(task_id, {"unexpected": True})
    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.FAILED, "verification")


async def test_handed_to_human__cancelled_by_the_person__cancelled() -> None:
    office_ = Office(receipts_action(BROWSER), recorded_browser(RecipeOutcome.challenge()))
    await office_.gateway.submit(ask())
    [task_id] = office_.human.tasks

    office_.human.cancel(task_id, "not needed")
    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.FAILED, "cancelled")
    assert office_.store.only().state is ActionState.CANCELLED


async def test_human_channel__creates_a_task_for_the_action() -> None:
    office_ = Office(receipts_action({"channel": "human", "recipe": "check_orders"}),
                     HumanChannel())  # fmt: skip

    result = await office_.gateway.submit(ask(actor=EMPLOYEE))

    assert result.outcome is ActionOutcome.HANDED_TO_HUMAN
    [task] = office_.human.tasks.values()
    assert task.reason == "human_channel"


async def test_browser_error_outcome__failed_not_retried() -> None:
    channel = recorded_browser(RecipeOutcome.failed("selector_missing"))
    office_ = Office(receipts_action(BROWSER), channel)

    result = await office_.gateway.submit(ask())

    assert result.outcome is ActionOutcome.FAILED
    assert len(channel.calls) == 1


# --- AC-5.13.5: an API refusal never falls through to an unscoped browser ---------------------


@pytest.mark.ac("AC-5.13.5")
async def test_missing_capability__no_fall_through_to_a_browser_without_its_own_scope() -> None:
    browser = recorded_browser()
    office_ = Office(
        receipts_action(API, BROWSER),
        EtsyApiChannel(FakeMarketplace(), EtsyShops()),
        browser,
        browser=Browser(scopes={}),
        capabilities=set(),
    )

    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "scope_missing")
    assert browser.calls == []


@pytest.mark.ac("AC-5.13.5")
async def test_missing_capability__the_browser_with_its_own_recorded_scope_runs() -> None:
    browser = recorded_browser()
    office_ = Office(
        receipts_action(API, BROWSER),
        EtsyApiChannel(FakeMarketplace(), EtsyShops()),
        browser,
        capabilities=set(),
    )

    result = await office_.gateway.submit(ask())

    assert result.channel is Channel.BROWSER


@pytest.mark.ac("AC-5.13.5")
async def test_api_refusal_at_execution__failed_never_the_browser() -> None:
    marketplace = FakeMarketplace(fail=MarketplacePermissionError("forbidden", status=403))
    browser = recorded_browser()
    office_ = Office(receipts_action(API, BROWSER), EtsyApiChannel(marketplace, EtsyShops()),
                     browser)  # fmt: skip

    result = await office_.gateway.submit(ask())

    assert (result.outcome, result.channel) == (ActionOutcome.FAILED, Channel.API)
    assert browser.calls == []


# --- the API adapter --------------------------------------------------------------------------


async def test_api__receipts__mapped_without_buyer_data_and_filters_passed() -> None:
    marketplace = FakeMarketplace(receipts=(receipt(),))
    office_ = Office(receipts_action(API), EtsyApiChannel(marketplace, EtsyShops()))

    result = await office_.gateway.submit(
        ask(input={"limit": 10, "offset": 20, "min_last_modified": 1_760_000_000})
    )

    assert result.outcome is ActionOutcome.SUCCEEDED
    [(name, shop, filters)] = marketplace.calls
    assert (name, shop) == ("list_receipts", ETSY_SHOP)
    assert filters["limit"] == 10
    assert filters["offset"] == 20
    assert filters["min_last_modified"] == datetime.fromtimestamp(1_760_000_000, UTC)
    assert result.output is not None
    [row] = cast("list[dict[str, Any]]", result.output["receipts"])
    assert row["receipt_id"] == 77
    assert row["grand_total"] == {"amount": 2500, "divisor": 100, "currency_code": "USD"}
    assert not {"name", "email", "buyer", "address"} & set(row)
    assert result.output["count"] == 1


async def test_api__shop__mapped() -> None:
    office_ = Office(
        real_catalog(office()),
        EtsyApiChannel(FakeMarketplace(), EtsyShops()),
        capabilities={(SHOP, "shop_details")},
    )

    result = await office_.gateway.submit(
        ask("etsy.shop.read", actor=ActorRef(ActorKind.SYSTEM, "probe"), key="shop:1", input={})
    )

    assert result.outcome is ActionOutcome.SUCCEEDED
    assert result.output == {"shop_id": ETSY_SHOP, "name": "LeatherTest", "currency": "USD"}


async def test_api__reviews__identifiers_and_stars_never_the_buyers_words() -> None:
    """Slice 6.9 (AC-6.9.2): `etsy.reviews.read` is an R0 GET; an R0 output may be stored, so
    the review's text (the buyer's words) never leaves the channel."""
    written = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    marketplace = FakeMarketplace(
        reviews=(
            ShopReview(listing_id=6, rating=2, language="en", created_at=written,
                       text="Fake review text: arrived late.", transaction_id=5),
            ShopReview(listing_id=7, rating=5, language=None, created_at=written),
        )
    )  # fmt: skip
    adapter = EtsyApiChannel(marketplace, EtsyShops())
    definition = real_catalog(office()).actions["etsy.reviews.read"]
    request = ask(
        "etsy.reviews.read",
        actor=ActorRef(ActorKind.AUTOMATION, "incident-detection-escalation"),
        key="reviews:1",
        input={"min_created": 1_761_000_000, "limit": 100, "offset": 0},
    )

    outcome = await adapter.execute(request, definition.bindings[0])

    assert definition.approval_policy.value == "R0"
    [(name, shop, filters)] = marketplace.calls
    assert (name, shop) == ("list_reviews", ETSY_SHOP)
    assert filters["min_created"] == datetime.fromtimestamp(1_761_000_000, UTC)
    assert (filters["limit"], filters["offset"]) == (100, 0)
    assert outcome.output == {
        "reviews": [
            {"listing_id": 6, "rating": 2, "created_at": written.isoformat(),
             "transaction_id": 5},
            {"listing_id": 7, "rating": 5, "created_at": written.isoformat()},
        ],
        "count": 2,
    }  # fmt: skip
    assert "arrived late" not in str(outcome)


def test_reviews__only_incident_detection_may_read_them_as_an_automation() -> None:
    from htco_application.workforce.files import read_registry  # noqa: PLC0415
    from tests.execution_fakes import REGISTRY  # noqa: PLC0415

    automations = read_registry(REGISTRY).registry().automations
    readers = sorted(a for a, d in automations.items() if "etsy.reviews.read" in d.allowed_actions)
    assert readers == ["incident-detection-escalation"]


async def test_api__a_write__refused_before_any_call() -> None:
    marketplace = FakeMarketplace()
    adapter = EtsyApiChannel(marketplace, EtsyShops())
    request = ask(
        "etsy.inventory.update", key="inventory:1", input={"listing_id": 1, "products": []}
    )
    binding = real_catalog(office()).actions["etsy.inventory.update"].bindings[0]

    with pytest.raises(MarketplaceWriteRefusedError, match="no recipe"):
        await adapter.execute(request, binding)

    assert marketplace.calls == []


async def test_api__a_disabled_write__never_routed_and_not_a_breaker_failure() -> None:
    """The inventory write is a disabled binding until its recipe is built (#147): it is never
    routed, so it never reaches Etsy and never counts against the breaker."""
    clock_now = NOW
    breakers = MemoryBreakers()
    office_ = Office(real_catalog(office()), EtsyApiChannel(FakeMarketplace(), EtsyShops()),
                     guards=Guards(breakers=breakers, rates=MemoryRateLimiter(),
                                   slots=MemorySlots()))  # fmt: skip
    write = ask(
        "etsy.inventory.update",
        actor=ActorRef(ActorKind.USER, str(USER)),
        key="inventory:2",
        owner_trigger=True,
        input={"listing_id": 1, "products": []},
    )

    for n in range(5):  # as many refusals as failures would open the breaker
        result = await office_.gateway.submit(replace(write, idempotency_key=f"inventory:{n}"))
        assert (result.outcome, result.error_code) == (ActionOutcome.DENIED, "no_channel")

    assert breakers.admit("etsy:api", now=clock_now) is None


async def test_api__a_shop_without_an_etsy_connection__not_found() -> None:
    adapter = EtsyApiChannel(FakeMarketplace(), EtsyShops({}))

    with pytest.raises(Exception, match="no Etsy connection") as raised:
        await adapter.execute(ask(), receipts_action(API).actions["etsy.receipts.read"].bindings[0])

    assert type(raised.value).__name__ == "MarketplaceNotFoundError"


async def test_internal__an_unknown_recipe__refused() -> None:
    with pytest.raises(LookupError, match="no internal use case"):
        await internal().execute(ask(), ChannelBinding(Channel.INTERNAL, recipe="nope"))


async def test_fake_browser__preflight_reports_ready() -> None:
    report = await recorded_browser().preflight(uuid.uuid4())

    assert report.ok


async def test_api__one_receipt_by_id__read_with_get_receipt() -> None:
    marketplace = FakeMarketplace()
    office_ = Office(receipts_action(API), EtsyApiChannel(marketplace, EtsyShops()))

    result = await office_.gateway.submit(ask(input={"receipt_id": 91}))

    assert result.outcome is ActionOutcome.SUCCEEDED
    assert marketplace.calls == [("get_receipt", ETSY_SHOP, {"receipt_id": 91})]
    assert result.output is not None
    assert result.output["count"] == 1
    [row] = cast("list[dict[str, Any]]", result.output["receipts"])
    assert row["receipt_id"] == 91


async def test_handed_to_human__another_request_with_the_same_key__refused_and_task_kept() -> None:
    office_ = Office(receipts_action(BROWSER), recorded_browser(RecipeOutcome.challenge()))
    first = await office_.gateway.submit(ask())
    [task_id] = office_.human.tasks
    office_.human.complete(task_id, {"receipts": [], "count": 0})

    other = await office_.gateway.submit(ask(input={"limit": 99}))  # same key, another request
    done = await office_.gateway.submit(ask())

    assert (other.outcome, other.error_code) == (ActionOutcome.DENIED, "idempotency_conflict")
    assert (done.outcome, done.execution_id) == (ActionOutcome.SUCCEEDED, first.execution_id)


def test_gateway__a_browser_or_human_channel_needs_a_task_store() -> None:
    world = World()

    with pytest.raises(ValueError, match="task store"):
        ActionGatewayService(
            GatewayParts(
                catalog=receipts_action(BROWSER),
                registry=office,
                store=MemoryActionStore(),
                principals=world.principals,
                shops=Shops(),
                approvals=Approvals(),
                adapters={Channel.BROWSER: recorded_browser()},
                capabilities=Capabilities(),
                clock=lambda: NOW,
            )
        )


async def test_hand_off_without_a_task_store__failed_not_stranded() -> None:
    world = World(hand_off=True, human=None)

    result = await world.gateway.submit(ask())

    assert (result.outcome, result.error_code) == (ActionOutcome.FAILED, "no_task_store")
