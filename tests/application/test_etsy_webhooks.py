"""Etsy webhooks (slice 5.3): Standard Webhooks verification, the inbox, the routing of all 11
event types, and the worker's re-read. In-memory ports; all values are invented."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_webhooks import (
    INVENTORY_CHANGED,
    LISTING_CHANGED,
    MAX_BODY_BYTES,
    ORDER_INTAKE,
    OUT_OF_STOCK,
    ROUTES,
    WEBHOOK_JOB,
    Accepted,
    EngineOrderIntake,
    EtsyWebhookIngress,
    Rejection,
    ResourceKind,
    WebhookProcessor,
    WebhookRejectedError,
    WebhookSecret,
    WindowLimiter,
    delivery_key,
    parse_event,
    verify_signature,
)
from htco_application.jobs import DEFAULT_RETRY, ClaimedJob
from htco_application.kernel import JobError, event_payload
from htco_application.marketplace import (
    InventorySnapshot,
    ListingSnapshot,
    MarketplaceNotFoundError,
    ProductStock,
)
from htco_domain.automation import AutomationExecution, ExecutionState, TriggerKind
from tests.log_capture import capture_logs
from tests.webhook_fakes import (
    ETSY_SHOP,
    NOW,
    ORG,
    SECRET,
    SECRET_BYTES,
    SECRET_TEXT,
    SHOP,
    KnownShops,
    MemoryOutbox,
    MemoryWebhookInbox,
    body_of,
    recorded_events,
    signed,
)

pytestmark = pytest.mark.anyio

ALL_EVENTS = [
    "order.paid",
    "order.canceled",
    "order.shipped",
    "order.delivered",
    "listing.created",
    "listing.updated",
    "listing.deleted",
    "inventory.created",
    "inventory.updated",
    "inventory.deleted",
    "inventory.out_of_stock",
]
LISTING = 1500000001
PRODUCT = 2500000001
RECEIPT = 4000000001


# --- The secret --------------------------------------------------------------------------------


def test_secret__parses_whsec_base64_and_hides_itself() -> None:
    secret = WebhookSecret.parse(SECRET_TEXT)
    assert secret.key == SECRET_BYTES
    assert SECRET_TEXT[6:] not in repr(secret)
    assert "hidden" in repr(secret)


@pytest.mark.parametrize("text", ["whsec_not base64!", "whsec_", "whsec_c2hvcnQ="])
def test_secret__refuses_what_is_not_a_signing_secret(text: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        WebhookSecret.parse(text)


# --- Verification (AC-5.3.1) ------------------------------------------------------------------


def _body() -> bytes:
    return body_of(recorded_events()["order.paid"])


@pytest.mark.ac("AC-5.3.1")
def test_verify__a_valid_signature_passes() -> None:
    body = _body()
    request = verify_signature(SECRET, signed(body), body, now=NOW)
    assert request.webhook_id == "msg_fake0001"
    assert request.timestamp == NOW.replace(microsecond=0)


@pytest.mark.ac("AC-5.3.1")
def test_verify__any_v1_entry_may_match_during_a_secret_rotation() -> None:
    body = _body()
    headers = signed(body)
    headers["webhook-signature"] = "v1,AAAA " + headers["webhook-signature"]
    verify_signature(SECRET, headers, body, now=NOW)


@pytest.mark.ac("AC-5.3.1")
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("modified_body", Rejection.BAD_SIGNATURE),
        ("other_secret", Rejection.BAD_SIGNATURE),
        ("wrong_version", Rejection.BAD_SIGNATURE),
        ("no_signature", Rejection.MISSING_HEADERS),
        ("no_id", Rejection.MISSING_HEADERS),
        ("no_timestamp", Rejection.MISSING_HEADERS),
        ("timestamp_not_a_number", Rejection.BAD_TIMESTAMP),
        ("too_old", Rejection.STALE_TIMESTAMP),
        ("from_the_future", Rejection.STALE_TIMESTAMP),
        ("too_large", Rejection.TOO_LARGE),
    ],
)
def test_verify__refusals_name_their_reason(change: str, reason: Rejection) -> None:
    body = _body()
    headers = signed(body)
    if change == "modified_body":
        body = body.replace(b"order.paid", b"order.canceled")
    elif change == "other_secret":
        headers = signed(body, secret=WebhookSecret(b"another-fake-key-of-32-bytes-!!!"))
    elif change == "wrong_version":
        headers["webhook-signature"] = headers["webhook-signature"].replace("v1,", "v2,")
    elif change == "no_signature":
        del headers["webhook-signature"]
    elif change == "no_id":
        headers["webhook-id"] = " "
    elif change == "no_timestamp":
        del headers["webhook-timestamp"]
    elif change == "timestamp_not_a_number":
        headers["webhook-timestamp"] = "yesterday"
    elif change == "too_old":
        headers = signed(body, at=NOW - timedelta(minutes=5, seconds=1))
    elif change == "from_the_future":
        headers = signed(body, at=NOW + timedelta(minutes=5, seconds=1))
    elif change == "too_large":
        body = b"x" * (MAX_BODY_BYTES + 1)
        headers = signed(body)
    with pytest.raises(WebhookRejectedError) as raised:
        verify_signature(SECRET, headers, body, now=NOW)
    assert raised.value.reason is reason


@pytest.mark.ac("AC-5.3.1")
def test_verify__inside_the_five_minute_window_passes() -> None:
    body = _body()
    verify_signature(SECRET, signed(body, at=NOW - timedelta(minutes=5)), body, now=NOW)
    verify_signature(SECRET, signed(body, at=NOW + timedelta(minutes=4)), body, now=NOW)


def test_verify__header_names_are_case_insensitive() -> None:
    body = _body()
    headers = {name.upper(): value for name, value in signed(body).items()}
    verify_signature(SECRET, headers, body, now=NOW)


# --- Parsing and routing (AC-5.3.4) -----------------------------------------------------------


def test_routes__cover_exactly_the_eleven_etsy_events() -> None:
    assert sorted(ROUTES) == sorted(ALL_EVENTS)


@pytest.mark.ac("AC-5.3.4")
@pytest.mark.parametrize("event_type", ALL_EVENTS)
def test_parse__recorded_payloads_name_their_resource(event_type: str) -> None:
    event = parse_event(body_of(recorded_events()[event_type]))
    assert event.event_type == event_type
    assert event.etsy_shop_id == ETSY_SHOP
    route = ROUTES[event_type]
    if route.resource is ResourceKind.ORDER:
        assert event.receipt_id == RECEIPT  # from the resource URL: order events carry no ID
        assert event.resource_id == RECEIPT
    else:
        assert event.listing_id == LISTING
        assert event.resource_id == LISTING
    if route.resource is ResourceKind.INVENTORY:
        assert event.product_id == PRODUCT


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"event_type": "order.paid"}', b'{"shop_id": 1}',
     b'{"event_type": "DROP TABLE", "shop_id": 1}', b'{"event_type": "x.y", "shop_id": true}'],
)  # fmt: skip
def test_parse__what_is_not_an_etsy_event_is_refused(body: bytes) -> None:
    with pytest.raises(WebhookRejectedError) as raised:
        parse_event(body)
    assert raised.value.reason is Rejection.BAD_PAYLOAD


# --- The ingress -------------------------------------------------------------------------------


@dataclass
class World:
    inbox: MemoryWebhookInbox = field(default_factory=MemoryWebhookInbox)
    shops: KnownShops = field(default_factory=KnownShops)
    now: datetime = NOW

    @property
    def ingress(self) -> EtsyWebhookIngress:
        return self._ingress

    def __post_init__(self) -> None:
        self._ingress = EtsyWebhookIngress(
            secret=SECRET, inbox=self.inbox, shops=self.shops, clock=lambda: self.now
        )

    async def send(
        self, kind: str, *, webhook_id: str = "msg_fake0001", **changes: Any
    ) -> Accepted:
        body = body_of(recorded_events()[kind] | changes)
        return await self.ingress.receive(signed(body, webhook_id=webhook_id, at=self.now), body)


@pytest.mark.ac("AC-5.3.4")
@pytest.mark.parametrize("event_type", ALL_EVENTS)
async def test_ingress__each_event_type_is_stored_and_routed_to_one_job(event_type: str) -> None:
    world = World()

    accepted = await world.send(event_type)

    assert accepted.job_id is not None
    assert not accepted.duplicate
    [job] = world.inbox.queue.jobs.values()
    assert job.spec.kind == WEBHOOK_JOB
    assert (job.spec.organization_id, job.spec.shop_id) == (ORG, SHOP)
    assert job.spec.payload["event_type"] == event_type
    assert job.spec.correlation_id == "etsy-webhook:msg_fake0001"
    if ROUTES[event_type].resource is ResourceKind.ORDER:
        assert job.spec.idempotency_key == f"etsy:{SHOP}:{RECEIPT}:{event_type}:msg_fake0001"
        assert job.spec.payload["receipt_id"] == RECEIPT
    else:
        assert job.spec.payload["listing_id"] == LISTING
    [(_, stored, _)] = world.inbox.rows.values()
    assert (stored.source, stored.event_id, stored.event_type) == (
        "etsy",
        "msg_fake0001",
        event_type,
    )


@pytest.mark.ac("AC-5.3.1")
async def test_ingress__a_replayed_webhook_id_is_ignored() -> None:
    world = World()

    first = await world.send("order.paid")
    again = await world.send("order.paid")

    assert again.duplicate
    assert again.inbox_id == first.inbox_id
    assert len(world.inbox.rows) == 1
    assert len(world.inbox.queue.jobs) == 1


async def test_ingress__each_distinct_order_delivery_gets_its_own_job() -> None:
    """Codex post-merge review (owner gate v4): two deliveries for the same receipt and type are
    two changes (a second parcel), never one job; only the exact replay is suppressed."""
    world = World()

    first = await world.send("order.shipped", webhook_id="msg_fake0001")
    second = await world.send("order.shipped", webhook_id="msg_fake0002")
    replay = await world.send("order.shipped", webhook_id="msg_fake0002")

    assert len(world.inbox.rows) == 2  # both events are kept, the replay is not
    assert first.job_id != second.job_id
    assert replay.duplicate
    assert replay.job_id == second.job_id
    assert len(world.inbox.queue.jobs) == 2


def test_delivery_key__a_long_webhook_id_is_hashed_never_cut() -> None:
    long_a, long_b = "m" * 199 + "a", "m" * 199 + "b"
    assert delivery_key("msg_fake0001") == "msg_fake0001"
    assert delivery_key(long_a) != delivery_key(long_b)
    assert len(delivery_key(long_a)) <= 71


async def test_ingress__listing_changes_at_different_times_each_get_a_job() -> None:
    world = World()

    await world.send("listing.updated", webhook_id="msg_fake0001")
    world.now += timedelta(seconds=30)
    await world.send("listing.updated", webhook_id="msg_fake0002")

    assert len(world.inbox.queue.jobs) == 2


@pytest.mark.ac("AC-5.3.4")
async def test_ingress__an_unknown_event_type_is_stored_and_counted_with_no_job() -> None:
    world = World()

    accepted = await world.send("order.paid", event_type="shop.renamed")
    again = await world.send("order.paid", event_type="shop.renamed")

    assert accepted.job_id is None
    assert again.duplicate
    assert world.inbox.queue.jobs == {}
    [(_, stored, job)] = world.inbox.rows.values()
    assert stored.event_type == "shop.renamed"
    assert job is None
    assert world.ingress.unrouted["unknown_event_type"] == 1


async def test_ingress__a_shop_not_connected_to_htco_is_stored_with_no_job() -> None:
    world = World(shops=KnownShops({}))

    accepted = await world.send("order.paid")

    assert accepted.job_id is None
    [(_, stored, _)] = world.inbox.rows.values()
    assert stored.organization_id is None
    assert stored.shop_id is None
    assert world.ingress.unrouted["unknown_shop"] == 1


async def test_ingress__an_order_event_without_a_receipt_is_stored_with_no_job() -> None:
    world = World()

    accepted = await world.send("order.paid", resource_url="https://openapi.etsy.com/v3/x")

    assert accepted.job_id is None
    assert world.ingress.unrouted["no_resource_id"] == 1


@pytest.mark.ac("AC-5.3.1")
async def test_ingress__nothing_is_stored_before_the_signature_verifies() -> None:
    world = World()
    body = body_of(recorded_events()["order.paid"])
    headers = signed(body)

    with capture_logs() as logs, pytest.raises(WebhookRejectedError):
        await world.ingress.receive(headers, body + b" ")

    assert world.inbox.rows == {}
    assert "resource_url" not in logs.raw()


# --- Rate limit (AC-5.3.3) --------------------------------------------------------------------


@pytest.mark.ac("AC-5.3.3")
def test_limiter__over_the_limit_waits_until_the_window_ends_then_admits() -> None:
    limiter = WindowLimiter(2)
    assert limiter.admit("etsy", now=NOW) is None
    assert limiter.admit("etsy", now=NOW) is None
    wait = limiter.admit("etsy", now=NOW + timedelta(milliseconds=400))
    assert wait == pytest.approx(0.6)
    assert limiter.admit("other", now=NOW) is None  # per source
    assert limiter.admit("etsy", now=NOW + timedelta(seconds=1)) is None  # Etsy's retry


# --- The worker's job --------------------------------------------------------------------------


@dataclass
class FakeEngine:
    refuse: bool = False
    requests: list[TriggerRequest] = field(default_factory=list)

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        self.requests.append(request)
        if self.refuse:
            raise AutomationRefused("etsy-order-intake is not active (draft)")
        return AutomationExecution(
            id=uuid.uuid7(), automation_id=request.automation_id, automation_version=1,
            idempotency_key=request.idempotency_key, trigger=request.trigger,
            actor=request.actor, state=ExecutionState.PENDING, attempts=0,
            correlation_id=request.correlation_id, organization_id=request.organization_id,
            shop_id=request.shop_id,
        )  # fmt: skip


@dataclass
class FakeCatalog:
    listing: ListingSnapshot | None = field(
        default_factory=lambda: ListingSnapshot(
            LISTING, ETSY_SHOP, "active", 3, datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
        )
    )
    inventory: InventorySnapshot = field(
        default_factory=lambda: InventorySnapshot(
            LISTING, (ProductStock(PRODUCT, 0), ProductStock(PRODUCT + 1, 3))
        )
    )
    reads: list[str] = field(default_factory=list)

    async def get_listing(self, shop_id: int, listing_id: int) -> ListingSnapshot:
        self.reads.append(f"listing:{shop_id}:{listing_id}")
        if self.listing is None:
            raise MarketplaceNotFoundError("gone", status=404)
        return self.listing

    async def get_listing_inventory(self, shop_id: int, listing_id: int) -> InventorySnapshot:
        self.reads.append(f"inventory:{shop_id}:{listing_id}")
        return self.inventory


@dataclass
class Processing:
    engine: FakeEngine = field(default_factory=FakeEngine)
    catalog: FakeCatalog = field(default_factory=FakeCatalog)
    outbox: MemoryOutbox = field(default_factory=MemoryOutbox)
    world: World = field(default_factory=World)

    @property
    def processor(self) -> WebhookProcessor:
        return WebhookProcessor(
            orders=EngineOrderIntake(self.engine),
            catalog=self.catalog,
            shops=self.world.shops,
            outbox=self.outbox,
            marks=self.world.inbox,
            clock=lambda: NOW,
        )

    async def run(self, kind: str, **changes: Any) -> tuple[str, ClaimedJob]:
        await self.world.send(kind, **changes)
        stored = next(iter(self.world.inbox.queue.jobs.values()))
        job = ClaimedJob(
            id=stored.id, kind=stored.spec.kind, idempotency_key=stored.spec.idempotency_key,
            payload=stored.spec.payload, organization_id=stored.spec.organization_id,
            shop_id=stored.spec.shop_id, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
            lease_expires_at=NOW + timedelta(minutes=1),
            correlation_id=stored.spec.correlation_id,
        )  # fmt: skip
        return await self.processor.process(job), job

    def published(self) -> dict[str, dict[str, Any]]:
        """Each published event type with its facts (the envelope's payload)."""
        return {
            str(m.payload["event_type"]): dict(event_payload(m))
            for m in self.outbox.messages.values()
        }


@pytest.mark.ac("AC-5.3.4")
@pytest.mark.parametrize("event_type", ["order.paid", "order.canceled", "order.shipped",
                                        "order.delivered"])  # fmt: skip
async def test_process__order_events_trigger_etsy_order_intake_once_per_receipt(
    event_type: str,
) -> None:
    run = Processing()

    outcome, job = await run.run(event_type)

    assert outcome == "ingest_triggered"
    [request] = run.engine.requests
    assert request.automation_id == ORDER_INTAKE
    assert request.trigger is TriggerKind.EVENT
    assert request.idempotency_key == (
        f"etsy:{SHOP}:{RECEIPT}:{event_type}:etsy-webhook:msg_fake0001"
    )
    assert request.payload == {"receipt_id": RECEIPT, "event_type": event_type}
    assert (request.organization_id, request.shop_id) == (ORG, SHOP)
    assert request.correlation_id == "etsy-webhook:msg_fake0001"
    assert request.causation_id == f"job:{job.id}"
    assert run.catalog.reads == []  # Order Intake re-reads the receipt itself (slice 5.4)
    assert list(run.world.inbox.processed)  # the inbox event is processed


async def test_process__a_refused_intake_is_logged_and_never_retried() -> None:
    run = Processing(engine=FakeEngine(refuse=True))

    with capture_logs() as logs:
        outcome, _ = await run.run("order.paid")

    assert outcome == "ingest_refused"
    assert any(line["event"].startswith("etsy.webhook_order_not_ingested") for line in logs.lines())
    assert list(run.world.inbox.processed)


@pytest.mark.ac("AC-5.3.4")
@pytest.mark.parametrize("event_type", ["listing.created", "listing.updated", "listing.deleted"])
async def test_process__listing_events_reread_the_listing_and_publish_it(event_type: str) -> None:
    run = Processing()

    outcome, _ = await run.run(event_type)

    assert outcome == "published"
    assert run.catalog.reads == [f"listing:{ETSY_SHOP}:{LISTING}"]
    assert run.published()[LISTING_CHANGED] == {
        "webhook_event_type": event_type, "listing_id": LISTING,
        "state": "active", "listing_quantity": 3, "etsy_updated_at": "2026-10-24T08:00:00+00:00",
    }  # fmt: skip
    assert run.engine.requests == []


async def test_process__a_deleted_listing_is_confirmed_by_the_reread() -> None:
    run = Processing(catalog=FakeCatalog(listing=None))

    await run.run("listing.deleted")

    assert run.published()[LISTING_CHANGED]["state"] == "deleted"


async def test_process__a_listing_of_another_shop_publishes_nothing() -> None:
    other = ListingSnapshot(LISTING, ETSY_SHOP + 1, "active", 1, NOW)
    run = Processing(catalog=FakeCatalog(listing=other))

    with capture_logs() as logs:
        outcome, _ = await run.run("listing.updated")

    assert outcome == "skipped"
    assert run.outbox.messages == {}
    assert any(
        line["event"].startswith("etsy.webhook_listing_of_another_shop") for line in logs.lines()
    )


@pytest.mark.ac("AC-5.3.4")
@pytest.mark.parametrize("event_type", ["inventory.created", "inventory.updated",
                                        "inventory.deleted"])  # fmt: skip
async def test_process__inventory_events_reread_the_stock_and_publish_it(event_type: str) -> None:
    run = Processing()

    await run.run(event_type)

    assert run.catalog.reads == [
        f"listing:{ETSY_SHOP}:{LISTING}",
        f"inventory:{ETSY_SHOP}:{LISTING}",
    ]
    published = run.published()
    assert set(published) == {INVENTORY_CHANGED}
    facts = published[INVENTORY_CHANGED]
    assert facts["product_id"] == PRODUCT
    assert facts["product_found"] is True
    assert facts["total_quantity"] == 3
    assert facts["products"] == 2


@pytest.mark.ac("AC-5.3.4")
async def test_process__out_of_stock_is_published_when_the_reread_confirms_it() -> None:
    run = Processing()

    await run.run("inventory.out_of_stock")

    assert set(run.published()) == {INVENTORY_CHANGED, OUT_OF_STOCK}
    assert run.published()[OUT_OF_STOCK]["in_stock"] is False


async def test_process__out_of_stock_the_reread_does_not_confirm_is_not_published() -> None:
    stocked = InventorySnapshot(LISTING, (ProductStock(PRODUCT, 5),))
    run = Processing(catalog=FakeCatalog(inventory=stocked))

    await run.run("inventory.out_of_stock")

    assert set(run.published()) == {INVENTORY_CHANGED}


async def test_process__a_replayed_job_publishes_each_event_once() -> None:
    run = Processing()
    _, job = await run.run("listing.updated")

    await run.processor.process(job)

    assert len(run.outbox.messages) == 1


async def test_process__a_disconnected_shop_is_skipped() -> None:
    run = Processing()
    await run.world.send("listing.updated")
    run.world.shops.shops.clear()
    stored = next(iter(run.world.inbox.queue.jobs.values()))
    job = ClaimedJob(
        id=stored.id, kind=WEBHOOK_JOB, idempotency_key="k", payload=stored.spec.payload,
        organization_id=ORG, shop_id=SHOP, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW,
    )  # fmt: skip

    assert await run.processor.process(job) == "skipped"
    assert run.catalog.reads == []


@pytest.mark.parametrize(
    "payload", [{}, {"event_type": "shop.renamed"}, {"event_type": "order.paid"},
                {"event_type": "listing.updated"}],
)  # fmt: skip
async def test_process__a_job_without_its_ids_fails_with_a_safe_message(
    payload: dict[str, Any],
) -> None:
    run = Processing()
    job = ClaimedJob(
        id=uuid.uuid7(), kind=WEBHOOK_JOB, idempotency_key="k", payload=payload,
        organization_id=ORG, shop_id=SHOP, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW,
    )  # fmt: skip

    with pytest.raises(JobError):
        await run.processor.process(job)


async def test_process__a_job_needs_its_tenant() -> None:
    job = ClaimedJob(
        id=uuid.uuid7(), kind=WEBHOOK_JOB, idempotency_key="k",
        payload={"event_type": "order.paid"},
        organization_id=None, shop_id=None, lock_key=None, attempts=1, retry=DEFAULT_RETRY,
        lease_expires_at=NOW,
    )  # fmt: skip
    with pytest.raises(JobError, match="shop"):
        await Processing().processor.process(job)
