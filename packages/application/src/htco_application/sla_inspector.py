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

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from htco_application.automation.durable import Completed, HandlerContext, HandlerOutcome
from htco_application.fulfillment_schedule import INCIDENT_DETECTION
from htco_application.incidents import (
    EVENTS,
    SLA_BREACHED,
    Detection,
    IncidentService,
    ObservedDrift,
    drift_detection,
    order_resource,
)
from htco_application.jobs import JobError, JsonValue
from htco_application.messaging import OutboxMessage
from htco_application.notifications import NotificationRequest, NotificationService
from htco_application.review_alerts import low_review_alerts
from htco_domain.actors import ActorKind, ActorRef
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

DETECTOR = ActorRef(ActorKind.AUTOMATION, INCIDENT_DETECTION)
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

    def receipts_for_transactions(
        self, shop_id: uuid.UUID, transaction_ids: tuple[int, ...]
    ) -> Mapping[int, int]:
        """The stored order of each transaction, in this shop only (low-review alerts, 6.9)."""
        ...


type Offload = Callable[[Callable[[], Any]], Awaitable[Any]]


async def _inline(work: Callable[[], Any]) -> Any:
    return work()


def breach_event(  # noqa: PLR0913 - one breach's facts
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    order: AwaitingOrder,
    policy: SlaPolicy,
    deadline: datetime,
    correlation_id: str | None,
) -> OutboxMessage:
    payload: dict[str, JsonValue] = {
        "event_type": SLA_BREACHED,
        "receipt_id": order.receipt_id,
        "policy_id": str(policy.id),
        "policy_version": policy.version,
        "window": policy.window,
        "unit": policy.unit.value,
        "paid_at": order.paid_at.isoformat(),
        "deadline": deadline.isoformat(),
    }
    return OutboxMessage(
        organization_id=organization_id,
        shop_id=shop_id,
        destination=EVENTS,
        idempotency_key=f"{SLA_BREACHED}:{shop_id}:{order.receipt_id}:{policy.id}:{policy.version}",
        payload=payload,
        correlation_id=None if correlation_id is None else correlation_id[:200],
    )


@dataclass
class IncidentDetection:
    """The `incident-detection-escalation` handler."""

    store: SlaStore
    incidents: IncidentService
    clock: Callable[[], datetime]
    offload: Offload = _inline
    # Ship-by alerts (slice 6.9) through the notification path of 6.4; None: none raised.
    alerts: NotificationService | None = None
    ship_by_warning_days: int = SHIP_BY_WARNING_DAYS

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
            )
        )  # fmt: skip

    async def _inspect(self, ctx: HandlerContext, shop_id: uuid.UUID) -> HandlerOutcome:
        now = self.clock()
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
                        correlation_id=ctx.correlation_id,
                    ),
                ),
            )
            _, created = await self.incidents.open(detection, actor=DETECTOR)
            opened += created
        closed, escalated = await self._follow_up(ctx, shop_id, now)
        output: dict[str, JsonValue] = {
            "checked": len(awaiting),
            "opened": opened,
            "resolved": closed,
            "escalated": escalated,
        }
        if self.alerts is not None:
            output["ship_by_alerts"] = await self._ship_by(ctx, shop_id, policies, awaiting, now)
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
            if incident.cause is Cause.TRACKING_MISSING and incident.receipt_id in closed_orders:
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
        if ctx.shop_id is not None and drift.shop_id != ctx.shop_id:
            raise JobError("a drift belongs to the execution's shop")
        incident, created = await self.incidents.open(drift_detection(drift), actor=DETECTOR)
        return Completed({"incident_id": str(incident.id), "opened": int(created)})
