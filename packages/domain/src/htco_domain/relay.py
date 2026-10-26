"""The AutoVA tracking relay (slice 6.10): its item's states and the relay window.

A relay item opens once per paid order whose tracking has not reached Etsy within the relay
window, waits for the owner to paste the number from AutoVA, is handed off (to a person with a
copy button until the guarded write of 6.8 exists), and closes on evidence: tracking seen on the
Etsy receipt, an upload, or a cancelled order.
"""

import uuid
from enum import StrEnum

from htco_domain.fulfillment import BusinessCalendar, SlaPolicy, WindowUnit

# ASSUMPTIONS A-20 (the owner, 2026-10-25): 2 business days after payment for every supplier,
# well before the SLA incident; per supplier from Phase 13.
RELAY_WINDOW_DAYS = 2


class RelayState(StrEnum):
    AWAITING_TRACKING = "awaiting_tracking"
    HANDED_OFF = "handed_off"
    CLOSED = "closed"

    @property
    def is_open(self) -> bool:
        return self is not RelayState.CLOSED


class ClosedReason(StrEnum):
    TRACKING_ON_ETSY = "tracking_on_etsy"
    UPLOADED = "uploaded"
    CANCELLED = "cancelled"


def relay_window(calendar: BusinessCalendar | None = None) -> SlaPolicy:
    """The relay window as an SLA policy in the shop's business-day calendar."""
    return SlaPolicy(
        id=uuid.UUID(int=0),
        window=RELAY_WINDOW_DAYS,
        unit=WindowUnit.BUSINESS_DAYS,
        calendar=calendar or BusinessCalendar(),
    )
