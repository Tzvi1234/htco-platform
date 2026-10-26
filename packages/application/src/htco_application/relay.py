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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol

from htco_application.access.authentication import AuthContext
from htco_application.access.policy import Capability, Outcome, Scope, ShopRef, decide
from htco_application.audit import AuditRecord
from htco_application.notifications import NotificationRequest, NotificationService
from htco_application.shops import OrganizationRequiredError
from htco_domain.actors import ActorKind, ActorRef
from htco_domain.fulfillment import BusinessCalendar, TrackingNumber
from htco_domain.notifications import NotificationKind
from htco_domain.relay import ClosedReason, RelayState, relay_window

LIST_LIMIT = 200
# A pasted code is kept at most this long (Sage's security review of #159): an item handed off
# without tracking on Etsy for 30 days closes as expired, and its code goes with it.
RELAY_CODE_TTL = timedelta(days=30)

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

    def open_item(self, item: RelayItem) -> tuple[RelayItem, bool]:
        """Insert unless the order has an item already (one per order, ever): the order's
        stored item, and whether it is new."""
        ...

    def item(self, organization_id: uuid.UUID, item_id: uuid.UUID) -> RelayItem | None: ...

    def open_items(
        self,
        organization_id: uuid.UUID,
        *,
        shop_ids: frozenset[uuid.UUID] | None,
        limit: int,
        after: RelayItem | None = None,
    ) -> tuple[RelayItem, ...]:
        """Oldest first, by `(opened_at, id)`; `shop_ids` (a shop-scoped role's grants) is
        applied before `limit`; `after` continues from that item (a keyset page)."""
        ...

    def reveal_code(
        self, organization_id: uuid.UUID, item_id: uuid.UUID, *, audit: AuditRecord
    ) -> str | None:
        """The pasted code while the item waits for its upload, with the reveal's audit in the
        same transaction; None (and no audit) once it closed."""
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
    calendar: BusinessCalendar | None = None,
) -> int:
    """One relay item per awaiting order strictly past the relay window, counted in the shop's
    business days (its SLA policy's calendar), and one notification. The notification is asked
    for again while the item waits, so one that failed is sent by the next inspection; its key
    keeps it to one (Codex review of #159). Returns how many items opened."""
    window = relay_window(calendar)
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
        stored, created = await offload(partial(store.open_item, item))
        opened += created
        if stored.state is not RelayState.AWAITING_TRACKING:
            continue
        await alerts.notify(
            NotificationRequest(
                organization_id=organization_id,
                kind=NotificationKind.RELAY_TRACKING_NEEDED,
                dedup_key=f"relay:{shop_id}:{order.receipt_id}",
                params={"receipt_id": order.receipt_id, "relay_item_id": str(stored.id)},
                shop_id=shop_id,
                correlation_id=correlation_id,
            )
        )
    return opened


async def reconcile_relay_items(  # noqa: PLR0913 - the shop and its collaborators
    store: RelayStore,
    closed_orders: Callable[[uuid.UUID, tuple[int, ...]], Mapping[int, datetime]],
    *,
    organization_id: uuid.UUID,
    shop_id: uuid.UUID,
    now: datetime,
    tracked_orders: Callable[[uuid.UUID, tuple[int, ...]], frozenset[int]],
    offload: Offload = _inline,
) -> tuple[int, int, int]:
    """Close the shop's open items whose order was cancelled or refunded on Etsy (`cancelled`,
    Codex review of #159) and those handed off longer than `RELAY_CODE_TTL` (`expired`, Sage's
    security review). Closing deletes a pasted code. Every open item is reached, page by page
    (keyset on `(opened_at, id)`), so a full first page never hides a later one (Codex, #159).
    An item whose order already has tracking closes as `tracking_on_etsy`: one opened from a
    snapshot taken before Tracking Synchronization recorded it is never left behind (local Codex
    on #159). Returns (cancelled, expired, tracked)."""
    cancelled = expired = tracked = 0
    after: RelayItem | None = None
    while True:
        items: tuple[RelayItem, ...] = await offload(
            partial(
                store.open_items,
                organization_id,
                shop_ids=frozenset({shop_id}),
                limit=LIST_LIMIT,
                after=after,
            )
        )
        if not items:
            break
        receipts = tuple(i.receipt_id for i in items)
        done: Mapping[int, datetime] = await offload(partial(closed_orders, shop_id, receipts))
        shipped: frozenset[int] = await offload(partial(tracked_orders, shop_id, receipts))
        for item in items:
            reason = (
                ClosedReason.CANCELLED
                if item.receipt_id in done
                else ClosedReason.TRACKING_ON_ETSY
                if item.receipt_id in shipped
                else ClosedReason.EXPIRED
                if item.state is RelayState.HANDED_OFF and now - item.updated_at > RELAY_CODE_TTL
                else None
            )
            if reason is None:
                continue
            moved = await offload(partial(store.close, item.id, reason=reason, now=now))
            if moved is not None:
                cancelled += reason is ClosedReason.CANCELLED
                expired += reason is ClosedReason.EXPIRED
                tracked += reason is ClosedReason.TRACKING_ON_ETSY
        if len(items) < LIST_LIMIT:
            break
        after = items[-1]
    return cancelled, expired, tracked


def _organization(auth: AuthContext, organization_id: uuid.UUID | None) -> uuid.UUID:
    organization = organization_id or auth.default_organization()
    if organization is None:
        raise OrganizationRequiredError("name the organization")
    return organization


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


class RelayCursorError(ValueError):
    """The cursor names no relay item of the organization."""


def list_relay_items(
    auth: AuthContext,
    store: RelayStore,
    *,
    organization_id: uuid.UUID | None,
    now: datetime,
    after: uuid.UUID | None = None,
) -> tuple[RelayItem, ...]:
    """One page (`LIST_LIMIT`) of the open items of the shops the caller may act on (update
    incidents), oldest first; `after` (the last item of the previous page) continues it. Raises
    `PermissionError` for a role that may not, `RelayCursorError` for an unknown cursor."""
    organization_id = _organization(auth, organization_id)
    principal = auth.principal_for(organization_id)
    overall = decide(principal, Capability.UPDATE_INCIDENTS, shop=None, now=now)
    if overall.outcome is not Outcome.ALLOW:
        raise PermissionError("the role may not act on fulfillment")
    shops = principal.granted_shops if overall.scope is Scope.GRANTED_SHOPS else None
    start = None if after is None else store.item(organization_id, after)
    if after is not None and start is None:
        raise RelayCursorError("no such cursor")
    return store.open_items(organization_id, shop_ids=shops, limit=LIST_LIMIT, after=start)


def submit_tracking(  # noqa: PLR0913 - the caller, the item and the pasted tracking
    auth: AuthContext,
    store: RelayStore,
    *,
    organization_id: uuid.UUID | None,
    item_id: uuid.UUID,
    carrier: str,
    code: str,
    now: datetime,
    correlation_id: str | None,
) -> RelayItem:
    """The owner pastes the number from AutoVA. Raises `RelayItemNotVisibleError` (404),
    `PermissionError` (403), `TrackingFormatError` (422), `RelayItemMovedError` and
    `DuplicateTrackingError` (409)."""
    organization_id = _organization(auth, organization_id)
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


def relay_code(  # noqa: PLR0913 - the caller, the item and the request
    auth: AuthContext,
    store: RelayStore,
    *,
    organization_id: uuid.UUID | None,
    item_id: uuid.UUID,
    now: datetime,
    correlation_id: str | None,
) -> str:
    """The pasted code for the person who copies it into Etsy: buyer-data roles of the shop
    only, while the item is open, and every reveal audited (`relay.code_revealed`, without the
    code). Anyone else, or a closed item, gets `RelayItemNotVisibleError` (404), so the answer
    never says whether a code exists."""
    organization_id = _organization(auth, organization_id)
    item = store.item(organization_id, item_id)
    if item is None or not all(
        _allowed(auth, organization_id, item.shop_id, capability, now=now)
        for capability in (Capability.UPDATE_INCIDENTS, Capability.VIEW_BUYER_CONTACT)
    ):
        raise RelayItemNotVisibleError("no such relay item")
    if not item.state.is_open:
        raise RelayItemNotVisibleError("no such relay item")
    if item.state is RelayState.HANDED_OFF and now - item.updated_at > RELAY_CODE_TTL:
        # Past its time to live the code is gone even before the next inspection deletes it
        # (local Codex on #159): the item closes as expired, and nothing is revealed.
        store.close(item.id, reason=ClosedReason.EXPIRED, now=now)
        raise RelayItemNotVisibleError("no such relay item")
    reveal = AuditRecord.new(
        organization_id=organization_id,
        actor=ActorRef(ActorKind.USER, str(auth.user_id)),
        action="relay.code_revealed",
        entity_type="relay_item",
        entity_id=str(item.id),
        after={"carrier": item.carrier, "last4": item.last4, "receipt_id": item.receipt_id},
        correlation_id=correlation_id,
    )
    code = store.reveal_code(organization_id, item_id, audit=reveal)
    if code is None:
        raise RelayItemNotVisibleError("no such relay item")
    return code


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
