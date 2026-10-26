"""The AutoVA tracking relay (slice 6.10): its relay items, the Human-task path until Phase 8A.

The owner orders from suppliers through AutoVA and copies each tracking number into Etsy by hand
(A-05). A paid order without tracking past the relay window opens exactly one relay item
(AC-6.10.1) and notifies the Owners (`relay_tracking_needed`). The owner pastes the number from
AutoVA in the office: carrier and format are validated before anything else (AC-6.10.4). A number
the Etsy receipt already carries closes the item on that evidence and is never kept (AC-6.10.2);
otherwise the item is handed off to a person, who copies it into Etsy (AC-6.10.3: never silently).
The guarded upload `etsy.shipments.create` is slice 6.8; this module makes no Etsy call.

Tracking Synchronization closes the order's item once the tracking shows on the Etsy receipt
(`TrackingRecorder`). The relay item is Phase 6's own (audit P1-4); Phase 7's task store extends
it. The full code is buyer data (ADR-0018): kept only for the copy, read only by buyer-data
roles, never in an audit, an event or a log.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from datetime import datetime
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, ShopRef, decide
from htco_application.audit import AuditRecord
from htco_application.notifications import NotificationRequest, NotificationService
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import TrackingNumber
from htco_domain.notifications import NotificationKind
from htco_domain.relay import ClosedReason, RelayState, relay_window

LIST_LIMIT = 200

type Offload = Callable[[Callable[[], Any]], Awaitable[Any]]


async def _inline(work: Callable[[], Any]) -> Any:
    return work()


@dataclass(frozen=True)
class RelayItem:
    """One order's relay item. Identifiers, states and times; the code only as its last four."""

    id: uuid.UUID
    organization_id: uuid.UUID
    shop_id: uuid.UUID
    receipt_id: int
    state: RelayState
    paid_at: datetime
    opened_at: datetime
    updated_at: datetime
    carrier: str | None = None
    last4: str | None = None
    closed_reason: ClosedReason | None = None
    closed_at: datetime | None = None


class PaidOrder(Protocol):
    """An order awaiting tracking, as the inspection lists it (`sla_inspector.AwaitingOrder`)."""

    @property
    def receipt_id(self) -> int: ...

    @property
    def paid_at(self) -> datetime: ...


class RelayItemNotVisibleError(LookupError):
    """No such item for this caller: another organization's, an ungranted shop's, or none."""


class RelayItemMovedError(Exception):
    """The item is no longer waiting for its tracking (pasted already, or closed)."""

    def __init__(self, state: RelayState) -> None:
        super().__init__(f"the relay item is {state.value}")
        self.state = state


class DuplicateTrackingError(Exception):
    """This number is already pasted for another order of the shop."""


class RelayStore(Protocol):
    """Synchronous (psycopg's pool): the API runs it in a worker thread, the worker offloads it."""

    def open_item(self, item: RelayItem) -> bool:
        """Insert unless the order has an item already (one per order, ever)."""
        ...

    def item(self, organization_id: uuid.UUID, item_id: uuid.UUID) -> RelayItem | None: ...

    def open_items(
        self, organization_id: uuid.UUID, *, shop_ids: frozenset[uuid.UUID] | None, limit: int
    ) -> tuple[RelayItem, ...]:
        """Oldest first; `shop_ids` (a shop-scoped role's grants) is applied before `limit`."""
        ...

    def tracking_on_receipt(self, shop_id: uuid.UUID, receipt_id: int, fingerprint: bytes) -> bool:
        """Has Tracking Synchronization seen this number on the order's Etsy receipt?"""
        ...

    def hand_off(  # noqa: PLR0913 - the item, the tracking and the audit
        self,
        item_id: uuid.UUID,
        *,
        carrier: str,
        fingerprint: bytes,
        last4: str,
        code: str,
        now: datetime,
        audit: AuditRecord,
    ) -> RelayItem:
        """Record the pasted tracking and hand the item off, with its audit, in one
        transaction. Raises `RelayItemMovedError` or `DuplicateTrackingError`."""
        ...

    def close(
        self,
        item_id: uuid.UUID,
        *,
        reason: ClosedReason,
        now: datetime,
        audit: AuditRecord | None = None,
    ) -> RelayItem | None:
        """Close an open item and delete its code; None when it was not open."""
        ...

    def close_order(
        self, shop_id: uuid.UUID, receipt_id: int, *, reason: ClosedReason, now: datetime
    ) -> bool:
        """Close the order's open item, if any, and delete its code."""
        ...


RELAY = ActorRef(ActorKind.AUTOMATION, "incident-detection-escalation")


async def open_due_relay_items(  # noqa: PLR0913 - the order list and its collaborators
    store: RelayStore,
    alerts: NotificationService,
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    awaiting: tuple[PaidOrder, ...],
    now: datetime,
    correlation_id: str | None,
    new_id: Callable[[], uuid.UUID] = uuid.uuid7,
    offload: Offload = _inline,
) -> int:
    """One relay item per awaiting order strictly past the relay window, and one notification
    when it opens. Runs inside Incident Detection's inspection (slice 6.10)."""
    window = relay_window()
    opened = 0
    for order in awaiting:
        if not window.is_breached(order.paid_at, now=now):
            continue
        item = RelayItem(
            id=new_id(),
            organization_id=organization_id,
            shop_id=shop_id,
            receipt_id=order.receipt_id,
            state=RelayState.AWAITING_TRACKING,
            paid_at=order.paid_at,
            opened_at=now,
            updated_at=now,
        )
        created: bool = await offload(partial(store.open_item, item))
        if not created:
            continue
        opened += 1
        await alerts.notify(
            NotificationRequest(
                organization_id=organization_id,
                kind=NotificationKind.RELAY_TRACKING_NEEDED,
                dedup_key=f"relay:{shop_id}:{order.receipt_id}",
                params={"receipt_id": order.receipt_id, "relay_item_id": str(item.id)},
                shop_id=shop_id,
                correlation_id=correlation_id,
            )
        )
    return opened


def _allowed(
    auth: AuthContext,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID | None,
    capability: Capability,
    *,
    now: datetime,
) -> bool:
    principal = auth.principal_for(organization_id)
    shop = None if shop_id is None else ShopRef(id=shop_id, organization_id=organization_id)
    return decide(principal, capability, shop=shop, now=now).outcome is Outcome.ALLOW


def list_relay_items(
    auth: AuthContext, store: RelayStore, *, organization_id: uuid.UUID, now: datetime
) -> tuple[RelayItem, ...]:
    """The open items of the shops the caller may act on (update incidents). Raises
    `PermissionError` for a role that may not."""
    principal = auth.principal_for(organization_id)
    overall = decide(principal, Capability.UPDATE_INCIDENTS, shop=None, now=now)
    if overall.outcome is not Outcome.ALLOW:
        raise PermissionError("the role may not act on fulfillment")
    shops = principal.granted_shops if overall.scope is Scope.GRANTED_SHOPS else None
    return store.open_items(organization_id, shop_ids=shops, limit=LIST_LIMIT)


def submit_tracking(  # noqa: PLR0913 - the caller, the item and the pasted tracking
    auth: AuthContext,
    store: RelayStore,
    *,
    organization_id: uuid.UUID,
    item_id: uuid.UUID,
    carrier: str,
    code: str,
    now: datetime,
    correlation_id: str | None,
) -> RelayItem:
    """The owner pastes the number from AutoVA. Raises `RelayItemNotVisibleError` (404),
    `PermissionError` (403), `TrackingFormatError` (422), `RelayItemMovedError` and
    `DuplicateTrackingError` (409)."""
    item = store.item(organization_id, item_id)
    if item is None or not _allowed(
        auth, organization_id, item.shop_id, Capability.VIEW_INCIDENTS, now=now
    ):
        raise RelayItemNotVisibleError("no such relay item")
    for capability in (Capability.UPDATE_INCIDENTS, Capability.VIEW_BUYER_CONTACT):
        if not _allowed(auth, organization_id, item.shop_id, capability, now=now):
            raise PermissionError("the role may not paste tracking")
    tracking = TrackingNumber.parse(carrier, code)  # before anything else (AC-6.10.4)
    if item.state is not RelayState.AWAITING_TRACKING:
        raise RelayItemMovedError(item.state)
    actor = ActorRef(ActorKind.USER, str(auth.user_id))
    fingerprint = tracking.fingerprint(item.shop_id)
    if store.tracking_on_receipt(item.shop_id, item.receipt_id, fingerprint):
        closed = store.close(
            item.id,
            reason=ClosedReason.TRACKING_ON_ETSY,
            now=now,
            audit=_audit(item, actor, "relay.closed_on_etsy_tracking", tracking, correlation_id),
        )
        if closed is None:
            raise RelayItemMovedError(RelayState.CLOSED)
        return closed
    return store.hand_off(
        item.id,
        carrier=tracking.carrier.value,
        fingerprint=fingerprint,
        last4=tracking.last4,
        code=tracking.code,
        now=now,
        audit=_audit(item, actor, "relay.tracking_pasted", tracking, correlation_id),
    )


def _audit(
    item: RelayItem,
    actor: ActorRef,
    action: str,
    tracking: TrackingNumber,
    correlation_id: str | None,
) -> AuditRecord:
    """Never the code itself: the carrier and its last four characters."""
    return AuditRecord.new(
        organization_id=item.organization_id,
        actor=actor,
        action=action,
        entity_type="relay_item",
        entity_id=str(item.id),
        after={
            "carrier": tracking.carrier.value,
            "last4": tracking.last4,
            "receipt_id": item.receipt_id,
        },
        correlation_id=correlation_id,
    )
