"""Tracking Synchronization (slice 6.1) through the real Action Gateway and its GET-only API
channel, with in-memory stores. All values are invented."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import MappingProxyType
from typing import Any

import pytest

from htco_application.automation.durable import Completed, HandlerContext
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_webhooks import WEBHOOK_ACTOR
from htco_application.execution.channels import EtsyApiChannel
from htco_application.execution.gateway import Channel
from htco_application.execution.service import ActionGatewayService, GatewayParts
from htco_application.fulfillment_schedule import TRACKING_SYNC
from htco_application.incidents import (
    INCIDENT_RESOLVED,
    Detection,
    IncidentService,
    order_resource,
)
from htco_application.jobs import JobError
from htco_application.maintenance import TenantShop
from htco_application.marketplace import OrderShipment
from htco_application.tracking_sync import (
    TRACKING_DETECTED,
    EngineTrackingSync,
    ObservedVia,
    TrackingRecorder,
    TrackingSource,
    TrackingSync,
)
from htco_application.workforce.registry import Registry, parse_automation
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationExecution, TriggerKind
from htco_domain.fulfillment import tracking_fingerprint
from htco_domain.incidents import Cause, IncidentState, ResourceKind
from htco_domain.integrations import IntegrationStatus
from tests.application.test_action_gateway import ORG, SHOP, office
from tests.application.test_execution_router import EtsyShops, FakeMarketplace
from tests.application.test_order_intake import _vault
from tests.automation_fakes import engine_document
from tests.etsy_fakes import Vault
from tests.execution_fakes import Approvals, Capabilities, MemoryActionStore, Principals, Shops
from tests.execution_fakes import real_catalog as catalog_of
from tests.fulfillment_fakes import MemoryIncidentStore, MemoryTrackingStore
from tests.log_capture import capture_logs
from tests.order_intake_fakes import FAKE_TRACKING, NOW, paid_receipt, shipped

pytestmark = pytest.mark.anyio

RECEIPT = 4000000001
SECOND_CODE = "FAKE-INTAKE-TRACK-02"


def tracking_office() -> Registry:
    base = office()
    automation = parse_automation(
        engine_document(TRACKING_SYNC, allowed_actions=["etsy.receipts.read"])
    )
    return replace(
        base, automations=MappingProxyType({**base.automations, TRACKING_SYNC: automation})
    )


def missing_detection(receipt_id: int = RECEIPT) -> Detection:
    return Detection(
        organization_id=ORG,
        cause=Cause.TRACKING_MISSING,
        resource_kind=ResourceKind.ORDER,
        resource_id=order_resource(SHOP, receipt_id),
        detected_at=NOW - timedelta(hours=1),
        source="sla_inspector",
        shop_id=SHOP,
        receipt_id=receipt_id,
    )


@dataclass
class Sync:
    marketplace: FakeMarketplace = field(default_factory=FakeMarketplace)
    tracking: MemoryTrackingStore = field(default_factory=MemoryTrackingStore)
    incident_store: MemoryIncidentStore = field(default_factory=MemoryIncidentStore)
    vault: Vault = field(default_factory=_vault)
    action_store: MemoryActionStore = field(default_factory=MemoryActionStore)

    @property
    def gateway(self) -> ActionGatewayService:
        registry = tracking_office()
        return ActionGatewayService(
            GatewayParts(
                catalog=catalog_of(registry),
                registry=lambda: registry,
                store=self.action_store,
                principals=Principals({}),
                shops=Shops({}),
                approvals=Approvals(),
                adapters={Channel.API: EtsyApiChannel(self.marketplace, EtsyShops())},
                capabilities=Capabilities({(SHOP, "receipts_read")}),
                clock=lambda: NOW,
            )
        )

    @property
    def incidents(self) -> IncidentService:
        return IncidentService(self.incident_store, clock=lambda: NOW)

    @property
    def handler(self) -> TrackingSync:
        recorder = TrackingRecorder(self.tracking, self.incidents, clock=lambda: NOW)
        return TrackingSync(recorder, self.tracking, self.vault, clock=lambda: NOW)

    def context(self, payload: dict[str, Any], *, attempt: int = 1) -> HandlerContext:
        return HandlerContext(
            execution_id=uuid.uuid7(),
            automation_id=TRACKING_SYNC,
            actor=ActorRef(ActorKind.SYSTEM, "etsy-webhook"),
            organization_id=ORG,
            shop_id=SHOP,
            correlation_id="etsy-webhook:msg_9",
            causation_id="job:9",
            attempt=attempt,
            payload=payload,
            gateway=self.gateway,
        )


def two_shipments() -> Any:
    receipt = shipped(paid_receipt(RECEIPT), minutes=9)
    return replace(
        receipt,
        shipments=(
            *receipt.shipments,
            OrderShipment("Yun Express", SECOND_CODE, receipt.created_at),
        ),
    )


@pytest.mark.ac("AC-6.1.1")
async def test_ac_6_1_1_each_shipment_once_however_often_it_is_observed() -> None:
    sync = Sync(FakeMarketplace(order_receipts=(two_shipments(),)))

    first = await sync.handler(sync.context({"receipt_id": RECEIPT, "event_type": "order.shipped"}))
    for attempt in range(2, 7):
        await sync.handler(
            sync.context({"receipt_id": RECEIPT, "event_type": "order.shipped"}, attempt=attempt)
        )

    assert first == Completed({"receipts": 1, "recorded": 2, "via": "webhook"})
    assert len(sync.tracking.rows) == 2
    assert len(sync.tracking.events.of(TRACKING_DETECTED)) == 2
    # Read only: every call is the GET of the receipt.
    assert {call[0] for call in sync.marketplace.calls} == {"get_order_receipt"}


@pytest.mark.ac("AC-6.1.2")
async def test_ac_6_1_2_the_first_sighting_keeps_its_time_source_and_trigger() -> None:
    sync = Sync(FakeMarketplace(order_receipts=(shipped(paid_receipt(RECEIPT), minutes=9),)))

    await sync.handler(sync.context({"receipt_id": RECEIPT, "event_type": "order.shipped"}))

    [row] = sync.tracking.rows.values()
    assert row.observed_at == NOW
    assert row.source is TrackingSource.ETSY
    assert row.observed_via is ObservedVia.WEBHOOK
    assert row.fingerprint == tracking_fingerprint(SHOP, FAKE_TRACKING)
    assert row.last4 == "CK01"
    assert FAKE_TRACKING not in repr(row)
    [event] = sync.tracking.events.of(TRACKING_DETECTED)
    assert event.payload["source"] == "etsy"
    assert event.payload["observed_via"] == "webhook"
    assert FAKE_TRACKING.replace("-", "") not in str(event.payload)


@pytest.mark.ac("AC-6.3.2")
async def test_ac_6_3_2_tracking_resolves_the_open_incident_with_its_evidence() -> None:
    sync = Sync(FakeMarketplace(order_receipts=(shipped(paid_receipt(RECEIPT), minutes=9),)))
    incident, _ = await sync.incidents.open(missing_detection(), actor=WEBHOOK_ACTOR)

    await sync.handler(sync.context({"receipt_id": RECEIPT, "event_type": "order.shipped"}))

    resolved = sync.incident_store.incidents[incident.id]
    assert resolved.state is IncidentState.RESOLVED
    last = sync.incident_store.steps[-1]
    assert last.evidence is not None
    assert last.evidence.source == "tracking:etsy"
    [row] = sync.tracking.rows.values()
    assert last.evidence.reference == str(row.id)
    [event] = sync.incident_store.events.of(INCIDENT_RESOLVED)
    assert event.payload["evidence_kind"] == "tracking_present"


async def test_a_receipt_without_shipments_records_nothing_and_resolves_nothing() -> None:
    sync = Sync(FakeMarketplace(order_receipts=(paid_receipt(RECEIPT),)))
    incident, _ = await sync.incidents.open(missing_detection(), actor=WEBHOOK_ACTOR)

    outcome = await sync.handler(sync.context({"receipt_id": RECEIPT}))

    assert outcome == Completed({"receipts": 1, "recorded": 0, "via": "webhook"})
    assert sync.incident_store.incidents[incident.id].state is IncidentState.DETECTED


async def test_a_shipment_with_an_empty_code_is_skipped() -> None:
    receipt = replace(
        shipped(paid_receipt(RECEIPT), minutes=9),
        shipments=(OrderShipment("usps", "  ", NOW),),
    )
    sync = Sync(FakeMarketplace(order_receipts=(receipt,)))

    outcome = await sync.handler(sync.context({"receipt_id": RECEIPT}))

    assert outcome == Completed({"receipts": 1, "recorded": 0, "via": "webhook"})


async def test_the_schedule_reads_only_shipped_orders_without_tracking() -> None:
    other = 4000000002
    sync = Sync(
        FakeMarketplace(
            order_receipts=(
                shipped(paid_receipt(RECEIPT), minutes=9),
                shipped(paid_receipt(other), minutes=9),
            )
        )
    )
    sync.tracking.shipped[SHOP] = [RECEIPT, other]

    first = await sync.handler(sync.context({"mode": "schedule"}))
    second = await sync.handler(sync.context({"mode": "schedule"}))

    assert first == Completed({"receipts": 2, "recorded": 2, "via": "schedule"})
    assert second == Completed({"receipts": 0, "recorded": 0, "via": "schedule"})
    [row, _] = sync.tracking.rows.values()
    assert row.observed_via is ObservedVia.SCHEDULE


async def test_a_disconnected_shop_is_skipped_without_a_read() -> None:
    sync = Sync(vault=_vault(IntegrationStatus.NEEDS_REAUTH))

    outcome = await sync.handler(sync.context({"receipt_id": RECEIPT}))

    assert outcome == Completed({"skipped": "not_connected"})
    assert sync.marketplace.calls == []


async def test_a_run_names_a_receipt_or_the_schedule() -> None:
    sync = Sync()
    with pytest.raises(JobError, match="receipt or the schedule"):
        await sync.handler(sync.context({"mode": "anything"}))
    with pytest.raises(JobError, match="names its shop"):
        await sync.handler(replace(sync.context({}), shop_id=None))


async def test_a_code_without_characters_is_refused_by_the_recorder() -> None:
    sync = Sync()
    recorder = TrackingRecorder(sync.tracking, sync.incidents, clock=lambda: NOW)
    with pytest.raises(JobError, match="without a tracking code"):
        await recorder.record(
            organization_id=ORG, shop_id=SHOP, receipt_id=RECEIPT, carrier_name="usps",
            code=" - ", source=TrackingSource.ETSY, observed_via=ObservedVia.SCHEDULE,
            notified_at=None, actor=WEBHOOK_ACTOR,
        )  # fmt: skip


@dataclass
class Engine:
    refuse: bool = False
    requests: list[TriggerRequest] = field(default_factory=list)

    def trigger(self, request: TriggerRequest) -> AutomationExecution:
        self.requests.append(request)
        if self.refuse:
            raise AutomationRefused("not_ready", "draft")
        return None  # type: ignore[return-value]


async def test_order_shipped_starts_tracking_sync_keyed_by_receipt_and_event() -> None:
    engine = Engine()
    trigger = EngineTrackingSync(engine)

    started = await trigger.receipt_event(
        TenantShop(ORG, SHOP), receipt_id=RECEIPT, event_type="order.shipped",
        correlation_id="c-1", causation_id="job:1",
    )  # fmt: skip

    assert started
    [request] = engine.requests
    assert request.automation_id == TRACKING_SYNC
    assert request.trigger is TriggerKind.EVENT
    assert request.idempotency_key == f"tracking:{SHOP}:{RECEIPT}:order.shipped"
    assert request.payload == {"receipt_id": RECEIPT, "event_type": "order.shipped"}


async def test_a_draft_tracking_sync_is_refused_and_logged() -> None:
    trigger = EngineTrackingSync(Engine(refuse=True))
    with capture_logs() as logs:
        started = await trigger.receipt_event(
            TenantShop(ORG, SHOP), receipt_id=RECEIPT, event_type="order.shipped",
            correlation_id="c-1", causation_id=None,
        )  # fmt: skip
    assert not started
    assert "fulfillment.tracking_not_started" in logs.raw()
