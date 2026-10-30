"""Incident Detection & Escalation (slices 6.2, 6.3, FR-INC-01): the
`incident-detection-escalation` handler. Deterministic: no model, no Etsy call.

- **Inspect** (`mode = "inspect"`, on the schedule, per shop): every stored order that is paid,
  not shipped, not cancelled or refunded and has no tracking event is checked against its SLA
  policy (`sla_policies`, the most specific for the shop, supplier and shipping method, else the
  generous default). An order past its deadline, strictly (AC-6.2.1), emits
  `fulfillment.sla_breached.v1` and opens one `tracking_missing` incident; a second breach of the
  same order finds the open incident and writes nothing (AC-6.3.1). An order cancelled or refunded
  meanwhile resolves its incident on that evidence. Then open incidents older than their severity
  allows escalate (`ESCALATED`, once).
- **A drift** (`drift` in the payload, from reconciliation, slice 5.5): one `sync_drift` incident
  per order, linked to the drift (AC-6.3.4).
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.etsy_webhooks import TriggersAutomations
from htco_application.fulfillment_schedule import INCIDENT_DETECTION
from htco_application.incidents import (
    SLA_BREACHED,
    Detection,
    IncidentService,
    ObservedDrift,
    drift_detection,
    order_resource,
)
from htco_application.kernel import (
    DomainEvent,
    JobError,
    JsonValue,
    Offload,
    OutboxMessage,
    event_payload,
    publish,
    run_inline,
)
from htco_application.notifications import NotificationRequest, NotificationService
from htco_application.relay import (
    RelayStore,
    open_due_relay_items,
    reconcile_relay_items,
)
from htco_application.review_alerts import low_review_alerts
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import TriggerKind
from htco_domain.fulfillment import SlaPolicy, select_policy, ship_by_at_risk
from htco_domain.incidents import (
    Cause,
    Evidence,
    EvidenceKind,
    IncidentState,
    ResourceKind,
    should_escalate,
)
from htco_domain.notifications import NotificationKind

_log = logging.getLogger(__name__)

DETECTOR = ActorRef(ActorKind.AUTOMATION, INCIDENT_DETECTION)
# Who starts it for a drift event (the platform, on the outbox's behalf).
DRIFT_TRIGGER_ACTOR = ActorRef(ActorKind.SYSTEM, "drift-events")
_DRIFT_FIELDS = ("shop_id", "receipt_id", "kind", "detected_at", "drift_id")
# Generous until the owner tunes it: one business day before the ship-by date.
SHIP_BY_WARNING_DAYS = 1


@dataclass(frozen=True)
class AwaitingOrder:
    """A stored order that has no tracking yet: identifiers and times only."""

    receipt_id: int
    paid_at: datetime
    supplier: str | None = None
    shipping_method: str | None = None
    # The earliest ship-by date of its items, as Etsy computes it (slice 6.9).
    expected_ship: datetime | None = None


class SlaStore(Protocol):
    """Synchronous (psycopg's pool); run through `offload`."""

    def policies(self, organization_id: uuid.UUID) -> tuple[SlaPolicy, ...]: ...

    def awaiting_tracking(self, shop_id: uuid.UUID) -> tuple[AwaitingOrder, ...]:
        """Paid, not shipped, not cancelled or refunded, no tracking event."""
        ...

    def closed_orders(
        self, shop_id: uuid.UUID, receipt_ids: tuple[int, ...]
    ) -> Mapping[int, datetime]:
        """Of these receipts, the cancelled or refunded ones, with Etsy's update time."""
        ...

    def tracked_orders(self, shop_id: uuid.UUID, receipt_ids: tuple[int, ...]) -> frozenset[int]:
        """Of these receipts, the ones with a tracking event (the AutoVA relay, 6.10)."""
        ...

    def receipts_for_transactions(
        self, shop_id: uuid.UUID, transaction_ids: tuple[int, ...]
    ) -> Mapping[int, int]:
        """The stored order of each transaction, in this shop only (low-review alerts, 6.9)."""
        ...

    def unlinked_drifts(self, shop_id: uuid.UUID, *, limit: int) -> tuple[StoredDrift, ...]:
        """The shop's drifts (`app.sync_drifts`) that no `sync_drift` incident of their order
        covers: none still open, and none resolved at or after the drift was detected (the one
        its event opened or found, or a later one). Oldest first, at most `limit`."""
        ...


@dataclass(frozen=True)
class StoredDrift:
    """A drift reconciliation recorded (`app.sync_drifts`): identifiers and its time only."""

    drift_id: uuid.UUID
    receipt_id: int
    kind: str
    detected_at: datetime


# Drifts opened per inspection by the backfill; the rest follow on the next run.
DRIFT_BACKFILL_LIMIT = 100


def breach_event(  # noqa: PLR0913 - one breach's facts
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    order: AwaitingOrder,
    policy: SlaPolicy,
    deadline: datetime,
    detected_at: datetime,
    correlation_id: str | None,
) -> OutboxMessage:
    payload: dict[str, JsonValue] = {
        "receipt_id": order.receipt_id,
        "policy_id": str(policy.id),
        "policy_version": policy.version,
        "window": policy.window,
        "unit": policy.unit.value,
        "paid_at": order.paid_at.isoformat(),
        "deadline": deadline.isoformat(),
    }
    return publish(
        DomainEvent(
            event_type=SLA_BREACHED,
            organization_id=organization_id,
            shop_id=shop_id,
            idempotency_key=(
                f"{SLA_BREACHED}:{shop_id}:{order.receipt_id}:{policy.id}:{policy.version}"
            ),
            occurred_at=detected_at,
            actor=DETECTOR,
            payload=payload,
            correlation_id=correlation_id,
        )
    )


@dataclass
class IncidentDetection:
    """The `incident-detection-escalation` handler."""

    store: SlaStore
    incidents: IncidentService
    clock: Callable[[], datetime]
    offload: Offload = run_inline
    # Ship-by alerts (slice 6.9) through the notification path of 6.4; None: none raised.
    alerts: NotificationService | None = None
    ship_by_warning_days: int = SHIP_BY_WARNING_DAYS
    # The AutoVA relay (slice 6.10): the inspection opens the due relay items; None: none open.
    relay: RelayStore | None = None
    # Waits out a short gateway deferral inside the low-review scan (6.9); tests pass a fake.
    sleep: Callable[[float], Awaitable[None]] | None = None

    async def __call__(self, ctx: HandlerContext) -> HandlerOutcome:
        drift = ctx.payload.get("drift")
        if isinstance(drift, dict):
            return await self._drift(ctx, drift)
        mode = ctx.payload.get("mode")
        if mode not in {"inspect", "reviews"}:
            raise JobError("an incident detection names the reviews or the inspect mode or a drift")
        if ctx.shop_id is None:
            raise JobError(f"the {mode} mode names its shop")
        if mode == "reviews":
            return await self._reviews(ctx, ctx.shop_id)
        return await self._inspect(ctx, ctx.shop_id)

    async def _reviews(self, ctx: HandlerContext, shop_id: uuid.UUID) -> HandlerOutcome:
        """Low-review alerts (slice 6.9, AC-6.9.2); without the alert service nothing is read."""
        if self.alerts is None:
            return Completed({"mode": "reviews", "reviews_read": 0, "low_review_alerts": 0})
        return Completed(
            await low_review_alerts(
                ctx, shop_id, orders=self.store, alerts=self.alerts,
                offload=self.offload, clock=self.clock,
                **({} if self.sleep is None else {"sleep": self.sleep}),
            )
        )  # fmt: skip

    async def _relay_cleanup(
        self, ctx: HandlerContext, shop_id: uuid.UUID, now: datetime
    ) -> tuple[int, int, int]:
        if self.relay is None:
            return 0, 0, 0
        return await reconcile_relay_items(
            self.relay, self.store.closed_orders, organization_id=ctx.organization_id,
            shop_id=shop_id, now=now, tracked_orders=self.store.tracked_orders,
            offload=self.offload,
        )  # fmt: skip

    async def _inspect(self, ctx: HandlerContext, shop_id: uuid.UUID) -> HandlerOutcome:
        now = self.clock()
        # The relay's retention first (local Codex on #159): a failing notification later in the
        # run never keeps a code past its time to live.
        cleaned = await self._relay_cleanup(ctx, shop_id, now)
        policies = await self.offload(lambda: self.store.policies(ctx.organization_id))
        awaiting = await self.offload(lambda: self.store.awaiting_tracking(shop_id))
        opened = 0
        for order in awaiting:
            policy = select_policy(
                policies,
                shop_id=shop_id,
                supplier=order.supplier,
                shipping_method=order.shipping_method,
            )
            if not policy.is_breached(order.paid_at, now=now):
                continue
            deadline = policy.deadline(order.paid_at)
            detection = Detection(
                organization_id=ctx.organization_id,
                cause=Cause.TRACKING_MISSING,
                resource_kind=ResourceKind.ORDER,
                resource_id=order_resource(shop_id, order.receipt_id),
                detected_at=now,
                source="sla_inspector",
                shop_id=shop_id,
                receipt_id=order.receipt_id,
                details={
                    "policy_id": str(policy.id),
                    "policy_version": policy.version,
                    "deadline": deadline.isoformat(),
                    "paid_at": order.paid_at.isoformat(),
                },
                correlation_id=ctx.correlation_id,
                events=(
                    breach_event(
                        organization_id=ctx.organization_id,
                        shop_id=shop_id,
                        order=order,
                        policy=policy,
                        deadline=deadline,
                        detected_at=now,
                        correlation_id=ctx.correlation_id,
                    ),
                ),
            )
            _, created = await self.incidents.open(detection, actor=DETECTOR)
            opened += created
        backfilled = await self._drift_backfill(ctx, shop_id)
        closed, escalated = await self._follow_up(ctx, shop_id, now)
        output: dict[str, JsonValue] = {
            "checked": len(awaiting),
            "opened": opened,
            "resolved": closed,
            "escalated": escalated,
        }
        if backfilled:
            output["drifts_backfilled"] = backfilled
        if self.alerts is not None:
            output["ship_by_alerts"] = await self._ship_by(ctx, shop_id, policies, awaiting, now)
            if self.relay is not None:
                output["relay_items_opened"] = await open_due_relay_items(
                    self.relay, self.alerts, organization_id=ctx.organization_id,
                    shop_id=shop_id, awaiting=awaiting, now=now,
                    correlation_id=ctx.correlation_id, offload=self.offload,
                    calendar=select_policy(policies, shop_id=shop_id).calendar,
                )  # fmt: skip
                # Again after opening: an item opened from this run's snapshot whose order got
                # its tracking meanwhile closes in the same run.
                again = await self._relay_cleanup(ctx, shop_id, now)
                cleaned = (cleaned[0] + again[0], cleaned[1] + again[1], cleaned[2] + again[2])
        if self.relay is not None:
            output["relay_items_cancelled"] = cleaned[0]
            output["relay_items_expired"] = cleaned[1]
            output["relay_items_tracked"] = cleaned[2]
        return Completed(output)

    async def _ship_by(
        self,
        ctx: HandlerContext,
        shop_id: uuid.UUID,
        policies: tuple[SlaPolicy, ...],
        awaiting: tuple[AwaitingOrder, ...],
        now: datetime,
    ) -> int:
        """One alert per order once it is inside the warning window before its ship-by date
        (AC-6.9.1), counted in the shop's business days (the calendar of its SLA policy)."""
        if self.alerts is None:  # pragma: no cover - checked by the caller
            return 0
        raised = 0
        for order in awaiting:
            if order.expected_ship is None:
                continue
            calendar = select_policy(policies, shop_id=shop_id).calendar
            if not ship_by_at_risk(
                order.expected_ship,
                now=now,
                warning_business_days=self.ship_by_warning_days,
                calendar=calendar,
            ):
                continue
            raised += (
                await self.alerts.notify(
                    NotificationRequest(
                        organization_id=ctx.organization_id,
                        kind=NotificationKind.ALERT_SHIP_BY,
                        dedup_key=f"ship_by:{shop_id}:{order.receipt_id}",
                        params={
                            "receipt_id": order.receipt_id,
                            # The shop's own day, as the warning window counts it.
                            "ship_by": order.expected_ship.astimezone(ZoneInfo(calendar.timezone))
                            .date()
                            .isoformat(),
                        },
                        shop_id=shop_id,
                        correlation_id=ctx.correlation_id,
                    )
                )
                > 0
            )
        return raised

    async def _follow_up(
        self, ctx: HandlerContext, shop_id: uuid.UUID, now: datetime
    ) -> tuple[int, int]:
        """Resolve missing-tracking incidents of orders cancelled or refunded meanwhile, and
        escalate incidents older than their severity allows."""
        incidents = await self.incidents.open_incidents(ctx.organization_id, shop_id)
        missing = tuple(
            i.receipt_id
            for i in incidents
            if i.cause is Cause.TRACKING_MISSING and i.receipt_id is not None
        )
        closed_orders = (
            await self.offload(lambda: self.store.closed_orders(shop_id, missing))
            if missing
            else {}
        )
        resolved = escalated = 0
        for incident in incidents:
            if (
                incident.cause is Cause.TRACKING_MISSING
                and incident.receipt_id is not None
                and incident.receipt_id in closed_orders
            ):
                await self.incidents.resolve_on_evidence(
                    ctx.organization_id,
                    incident.dedup_key,
                    Evidence(
                        EvidenceKind.ORDER_CLOSED,
                        closed_orders[incident.receipt_id],
                        "etsy_receipt",
                        f"receipt:{incident.receipt_id}",
                    ),
                    actor=DETECTOR,
                    correlation_id=ctx.correlation_id,
                )
                resolved += 1
            elif should_escalate(
                incident.state, incident.severity, detected_at=incident.detected_at, now=now
            ):
                await self.incidents.move(
                    ctx.organization_id,
                    incident.id,
                    IncidentState.ESCALATED,
                    actor=DETECTOR,
                    note="age_over_severity_limit",
                    correlation_id=ctx.correlation_id,
                )
                escalated += 1
        return resolved, escalated

    async def _drift_backfill(self, ctx: HandlerContext, shop_id: uuid.UUID) -> int:
        """Open the incident of every drift no incident covers yet (AC-6.3.4; Codex post-merge
        review, owner gate v4). A drift event that reached the subscriber while this automation
        was draft was refused and acknowledged for good, and reconciliation never emits it again:
        the first inspection after the activation opens its incident, from the stored drift, with
        the same detection as the event's run. Deterministic and idempotent: an incident opened
        here is linked by the drift's ID, so the next run finds nothing."""
        drifts = await self.offload(
            lambda: self.store.unlinked_drifts(shop_id, limit=DRIFT_BACKFILL_LIMIT)
        )
        opened = 0
        for stored in drifts:
            drift = ObservedDrift(
                organization_id=ctx.organization_id,
                shop_id=shop_id,
                receipt_id=stored.receipt_id,
                kind=stored.kind,
                detected_at=stored.detected_at,
                drift_id=stored.drift_id,
                correlation_id=ctx.correlation_id,
            )
            _, created = await self.incidents.open(drift_detection(drift), actor=DETECTOR)
            opened += created
        return opened

    async def _drift(self, ctx: HandlerContext, raw: Mapping[str, JsonValue]) -> HandlerOutcome:
        try:
            drift = ObservedDrift(
                organization_id=ctx.organization_id,
                shop_id=uuid.UUID(str(raw["shop_id"])),
                receipt_id=int(str(raw["receipt_id"])),
                kind=str(raw["kind"]),
                detected_at=datetime.fromisoformat(str(raw["detected_at"])),
                drift_id=uuid.UUID(str(raw["drift_id"])),
                correlation_id=ctx.correlation_id,
            )
        except KeyError, ValueError:
            raise JobError("a drift names its shop, receipt, kind, time and ID") from None
        if ctx.shop_id is None or drift.shop_id != ctx.shop_id:  # fails closed (quill, #129)
            raise JobError("a drift belongs to the execution's shop")
        incident, created = await self.incidents.open(drift_detection(drift), actor=DETECTOR)
        return Completed({"incident_id": str(incident.id), "opened": int(created)})


@dataclass
class DriftIncidents:
    """`orders.drift_detected.v1` → one Incident Detection & Escalation run per drift (AC-6.3.4),
    which opens one `sync_drift` incident per order. Refused (and logged) until the owner
    activates it; an event that names no drift (one from before slice 6.7) starts nothing."""

    engine: TriggersAutomations
    offload: Offload = run_inline

    @staticmethod
    def request_for(message: OutboxMessage) -> TriggerRequest | None:
        payload = event_payload(message)
        drift = {name: payload.get(name) for name in _DRIFT_FIELDS}
        if (
            message.shop_id is None
            or not isinstance(drift["receipt_id"], int)
            or not all(isinstance(drift[name], str) for name in ("kind", "detected_at", "drift_id"))
        ):
            return None
        cause = f"outbox:{message.idempotency_key}"[:200]
        return TriggerRequest(
            automation_id=INCIDENT_DETECTION,
            trigger=TriggerKind.EVENT,
            actor=DRIFT_TRIGGER_ACTOR,
            idempotency_key=f"{INCIDENT_DETECTION}:drift:{drift['drift_id']}"[:200],
            payload={"drift": drift},
            correlation_id=message.correlation_id or cause,
            organization_id=message.organization_id,
            shop_id=message.shop_id,
            causation_id=cause,
        )

    async def handle(self, message: OutboxMessage) -> None:
        request = self.request_for(message)
        if request is None:
            return
        try:
            await self.offload(lambda: self.engine.trigger(request))
        except AutomationRefused:
            _log.warning("fulfillment.drift_not_started: shop=%s", message.shop_id)
