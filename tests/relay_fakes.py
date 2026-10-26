"""The AutoVA relay's store in memory (slice 6.10)."""

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime

from htco_application.audit import AuditRecord
from htco_application.relay import (
    DuplicateTrackingError,
    RelayItem,
    RelayItemMovedError,
)
from htco_domain.relay import ClosedReason, RelayState


@dataclass
class MemoryRelayStore:
    items: dict[uuid.UUID, RelayItem] = field(default_factory=dict)
    codes: dict[uuid.UUID, str] = field(default_factory=dict)
    fingerprints: dict[uuid.UUID, bytes] = field(default_factory=dict)
    # (shop, receipt, fingerprint): tracking already on the Etsy receipt (`tracking_events`).
    on_etsy: set[tuple[uuid.UUID, int, bytes]] = field(default_factory=set)
    audits: list[AuditRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def open_item(self, item: RelayItem) -> tuple[RelayItem, bool]:
        with self._lock:
            found = next(
                (
                    i
                    for i in self.items.values()
                    if i.shop_id == item.shop_id and i.receipt_id == item.receipt_id
                ),
                None,
            )
            if found is not None:
                return found, False
            self.items[item.id] = item
            return item, True

    def item(self, organization_id: uuid.UUID, item_id: uuid.UUID) -> RelayItem | None:
        found = self.items.get(item_id)
        return found if found is not None and found.organization_id == organization_id else None

    def open_items(
        self, organization_id: uuid.UUID, *, shop_ids: frozenset[uuid.UUID] | None, limit: int
    ) -> tuple[RelayItem, ...]:
        found = [
            i
            for i in self.items.values()
            if i.organization_id == organization_id
            and i.state.is_open
            and (shop_ids is None or i.shop_id in shop_ids)
        ]
        return tuple(sorted(found, key=lambda i: i.opened_at)[:limit])

    def reveal_code(
        self, organization_id: uuid.UUID, item_id: uuid.UUID, *, audit: AuditRecord
    ) -> str | None:
        found = self.item(organization_id, item_id)
        code = None if found is None else self.codes.get(item_id)
        if code is not None:
            self.audits.append(audit)
        return code

    def tracking_on_receipt(self, shop_id: uuid.UUID, receipt_id: int, fingerprint: bytes) -> bool:
        return (shop_id, receipt_id, fingerprint) in self.on_etsy

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
        with self._lock:
            item = self.items[item_id]
            if item.state is not RelayState.AWAITING_TRACKING:
                raise RelayItemMovedError(item.state)
            if any(
                other.shop_id == item.shop_id and self.fingerprints.get(other.id) == fingerprint
                for other in self.items.values()
            ):
                raise DuplicateTrackingError
            moved = replace(
                item, state=RelayState.HANDED_OFF, carrier=carrier, last4=last4, updated_at=now
            )
            self.items[item_id] = moved
            self.fingerprints[item_id] = fingerprint
            self.codes[item_id] = code
            self.audits.append(audit)
            return moved

    def close(
        self,
        item_id: uuid.UUID,
        *,
        reason: ClosedReason,
        now: datetime,
        audit: AuditRecord | None = None,
    ) -> RelayItem | None:
        with self._lock:
            item = self.items.get(item_id)
            if item is None or not item.state.is_open:
                return None
            closed = replace(
                item, state=RelayState.CLOSED, closed_reason=reason, closed_at=now, updated_at=now
            )
            self.items[item_id] = closed
            self.codes.pop(item_id, None)
            if audit is not None:
                self.audits.append(audit)
            return closed

    def close_order(
        self, shop_id: uuid.UUID, receipt_id: int, *, reason: ClosedReason, now: datetime
    ) -> bool:
        found = next(
            (
                i
                for i in self.items.values()
                if i.shop_id == shop_id and i.receipt_id == receipt_id and i.state.is_open
            ),
            None,
        )
        return found is not None and self.close(found.id, reason=reason, now=now) is not None
