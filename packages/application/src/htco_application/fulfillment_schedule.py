"""The fulfillment schedule (slices 6.1, 6.2): every 15 minutes, one job per connected shop that
starts Tracking Synchronization (`mode = "schedule"`) and Incident Detection & Escalation
(`mode = "inspect"`) on the engine, each keyed by the shop and the period, so a second worker or a
replayed schedule starts nothing new.

Every hour (slice 6.9, AC-6.9.2), one job per connected shop starts Incident Detection & Escalation
in its `reviews` mode, the low-review read, under its own flag.

Until the owner activates an automation (slice 6.7) the engine refuses it: the refusal is logged
and the job succeeds, so nothing retries in a loop. The inspector schedule is also off unless
`HTCO_WORKER_SCHEDULES` names it (the phase's rollback: "disable the inspector schedule by flag").
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from htco_application.automation.engine import AutomationRefused, TriggerRequest
from htco_application.jobs import ClaimedJob, JobQueue, fan_out_per_shop
from htco_application.kernel import ConnectedShops, JobError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.automation import AutomationExecution, TriggerKind
from htco_domain.jobs import Schedule

# The automations it starts (their handlers live in `tracking_sync` and `sla_inspector`).
TRACKING_SYNC = "tracking-sync"
INCIDENT_DETECTION = "incident-detection-escalation"
FULFILLMENT_SCHEDULE = Schedule("fulfillment.inspect", timedelta(minutes=15))
FULFILLMENT_SHOP_JOB = "fulfillment.inspect_shop"
REVIEW_SCHEDULE = Schedule("fulfillment.reviews", timedelta(hours=1))
REVIEW_SHOP_JOB = "fulfillment.reviews_shop"
# The AutoVA relay's code retention (slice 6.10, local Codex on #159): its own hourly job, so a
# pasted code never outlives its time to live whatever the inspection does.
RELAY_RETENTION_SCHEDULE = Schedule("relay.code_retention", timedelta(hours=1))
RELAY_RETENTION_SHOP_JOB = "relay.code_retention_shop"
SCHEDULER = ActorRef(ActorKind.SYSTEM, "scheduler")

_log = logging.getLogger(__name__)


class Triggers(Protocol):
    def trigger(self, request: TriggerRequest) -> AutomationExecution: ...


def enqueue_fulfillment_inspections(
    shops: ConnectedShops, queue: JobQueue, *, now: datetime
) -> int:
    return _enqueue_per_shop(shops, queue, FULFILLMENT_SCHEDULE, FULFILLMENT_SHOP_JOB, now=now)


def enqueue_review_checks(shops: ConnectedShops, queue: JobQueue, *, now: datetime) -> int:
    return _enqueue_per_shop(shops, queue, REVIEW_SCHEDULE, REVIEW_SHOP_JOB, now=now)


def enqueue_relay_retention(shops: ConnectedShops, queue: JobQueue, *, now: datetime) -> int:
    return _enqueue_per_shop(
        shops, queue, RELAY_RETENTION_SCHEDULE, RELAY_RETENTION_SHOP_JOB, now=now
    )


def _enqueue_per_shop(
    shops: ConnectedShops, queue: JobQueue, schedule: Schedule, kind: str, *, now: datetime
) -> int:
    period = schedule.key_for(now).rsplit("@", 1)[-1]
    return fan_out_per_shop(
        queue,
        shops.connected_shops(),
        kind=kind,
        key=lambda shop: f"{kind}:{shop.shop_id}:{period}",
        lock=None,
        payload={"period": period},
        now=now,
    )


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


@dataclass
class ReviewCheck:
    """The hourly per-shop job: start Incident Detection & Escalation's `reviews` mode."""

    engine: Triggers

    def run(self, job: ClaimedJob) -> bool:
        if job.organization_id is None or job.shop_id is None:
            raise JobError("a review check names its shop and organization")
        period = job.payload.get("period")
        if not isinstance(period, str):
            raise JobError("a review check names its period")
        request = TriggerRequest(
            automation_id=INCIDENT_DETECTION,
            trigger=TriggerKind.SCHEDULE,
            actor=SCHEDULER,
            idempotency_key=f"{INCIDENT_DETECTION}:reviews:{job.shop_id}:{period}",
            payload={"mode": "reviews"},
            correlation_id=job.correlation_id or f"job:{job.id}",
            organization_id=job.organization_id,
            shop_id=job.shop_id,
            causation_id=f"job:{job.id}",
        )
        try:
            self.engine.trigger(request)
        except AutomationRefused:
            _log.warning("fulfillment.not_started: %s shop=%s", INCIDENT_DETECTION, job.shop_id)
            return False
        return True
