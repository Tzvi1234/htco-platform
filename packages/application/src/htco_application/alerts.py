"""Deterministic alerts (slice 6.9) on the one notification path of slice 6.4 (audit P2-2).

An alert is a notification of an alert kind; there is no alert table. The ship-by alert is
raised by Incident Detection & Escalation's inspection (`sla_inspector`); the stock alert by the
`catalog.out_of_stock.v1` event, which the webhook job publishes only after the re-read confirms
it (slice 5.3).
"""

import uuid
from dataclasses import dataclass

from htco_application.messaging import OutboxMessage
from htco_application.notifications import NotificationRequest, NotificationService
from htco_domain.notifications import NotificationKind

OUT_OF_STOCK_EVENT = "catalog.out_of_stock.v1"


@dataclass
class StockAlerts:
    """`catalog.out_of_stock.v1` → one alert per listing and Etsy change."""

    service: NotificationService

    async def handle(self, message: OutboxMessage) -> None:
        payload = message.payload
        listing = payload.get("listing_id")
        if not isinstance(listing, int) or isinstance(listing, bool) or message.shop_id is None:
            return
        changed = str(payload.get("etsy_updated_at") or "unknown")[:19]
        shop: uuid.UUID = message.shop_id
        await self.service.notify(
            NotificationRequest(
                organization_id=message.organization_id,
                kind=NotificationKind.ALERT_OUT_OF_STOCK,
                dedup_key=f"out_of_stock:{shop}:{listing}:{changed}"[:120],
                params={"listing_id": listing},
                shop_id=shop,
                correlation_id=message.correlation_id,
            )
        )
