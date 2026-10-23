"""The fulfillment schedule (slices 6.1, 6.2): every 15 minutes, one job per connected shop that
starts Tracking Synchronization (`mode = "schedule"`) and Incident Detection & Escalation
(`mode = "inspect"`) on the engine, each keyed by the shop and the period, so a second worker or a
replayed schedule starts nothing new.

Until the owner activates an automation (slice 6.7) the engine refuses it: the refusal is logged
and the job succeeds, so nothing retries in a loop. The inspector schedule is also off unless
`HTCO_WORKER_SCHEDULES` names it (the phase's rollback: "disable the inspector schedule by flag").
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.jobs import ClaimedJob, JobError, JobQueue, JobSpec
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationExecution, TriggerKind
from htco_domain.jobs import Schedule

# The automations it starts (their handlers live in `tracking_sync` and `sla_inspector`).
TRACKING_SYNC = "tracking-sync"
INCIDENT_DETECTION = "incident-detection-escalation"
FULFILLMENT_SCHEDULE = Schedule("fulfillment.inspect", timedelta(minutes=15))
FULFILLMENT_SHOP_JOB = "fulfillment.inspect_shop"
SCHEDULER = ActorRef(ActorKind.SYSTEM, "scheduler")

_log = logging.getLogger(__name__)


class ScheduledShop(Protocol):
    @property
    def organization_id(self) -> uuid.UUID: ...

    @property
    def shop_id(self) -> uuid.UUID: ...


class ConnectedShops(Protocol):
    """The maintenance `ShopDirectory`: connected shops by ID only, across organizations."""

    def connected_shops(self) -> Sequence[ScheduledShop]: ...


class Triggers(Protocol):
    def trigger(self, request: TriggerRequest) -> AutomationExecution: ...


def enqueue_fulfillment_inspections(
    shops: ConnectedShops, queue: JobQueue, *, now: datetime
) -> int:
    period = FULFILLMENT_SCHEDULE.key_for(now).rsplit("@", 1)[-1]
    listed = shops.connected_shops()
    for shop in listed:
        queue.enqueue(
            JobSpec(
                kind=FULFILLMENT_SHOP_JOB,
                idempotency_key=f"{FULFILLMENT_SHOP_JOB}:{shop.shop_id}:{period}",
                organization_id=shop.organization_id,
                shop_id=shop.shop_id,
                payload={"period": period},
            ),
            now=now,
        )
    return len(listed)


@dataclass
class FulfillmentInspection:
    """The per-shop job: start both automations for this period."""

    engine: Triggers

    def run(self, job: ClaimedJob) -> dict[str, bool]:
        if job.organization_id is None or job.shop_id is None:
            raise JobError("a fulfillment inspection names its shop and organization")
        period = job.payload.get("period")
        if not isinstance(period, str):
            raise JobError("a fulfillment inspection names its period")
        started: dict[str, bool] = {}
        for automation, mode in ((TRACKING_SYNC, "schedule"), (INCIDENT_DETECTION, "inspect")):
            request = TriggerRequest(
                automation_id=automation,
                trigger=TriggerKind.SCHEDULE,
                actor=SCHEDULER,
                idempotency_key=f"{automation}:{job.shop_id}:{period}",
                payload={"mode": mode},
                correlation_id=job.correlation_id or f"job:{job.id}",
                organization_id=job.organization_id,
                shop_id=job.shop_id,
                causation_id=f"job:{job.id}",
            )
            try:
                self.engine.trigger(request)
            except AutomationRefused:
                _log.warning("fulfillment.not_started: %s shop=%s", automation, job.shop_id)
                started[automation] = False
            else:
                started[automation] = True
        return started
