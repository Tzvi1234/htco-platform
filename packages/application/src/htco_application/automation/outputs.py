"""Which part of a handler's output a completed execution keeps (#127; sage and quill on #158).

A handler returns `Completed(output)`. The engine keeps only what an explicit typed model in
this module allows for that automation: a closed set of keys, each one a count or an ID (an
integer), one of a few codes, a flag, or a UUID. Nothing nested, no free text, so it can never
hold buyer data, and its size is bounded by the model. Every other key, and every value of the
wrong type, is dropped and logged with the automation's ID and how many were dropped, never a
value. An automation without a model keeps nothing. Per-listing maps such as Product Sales
Counters' `units` and `orders` are deliberately not kept: only its scalar totals.
"""

import logging
import re
from collections.abc import Callable, Mapping

from htco_application.etsy_webhooks import ORDER_INTAKE
from htco_application.fulfillment_schedule import INCIDENT_DETECTION, TRACKING_SYNC
from htco_application.jobs import JsonValue
from htco_application.order_automations import ORDER_DEDUP, SALES_COUNTERS

_log = logging.getLogger(__name__)

_MAX_INT = 2**53 - 1  # exact in JSON and in JavaScript
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

type Field = Callable[[JsonValue], bool]


def _count(value: JsonValue) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_INT


def _id(value: JsonValue) -> bool:
    return _count(value) and value != 0


def _flag(value: JsonValue) -> bool:
    return isinstance(value, bool)


def _uuid(value: JsonValue) -> bool:
    return isinstance(value, str) and _UUID.match(value) is not None


def _one_of(*codes: str | None) -> Field:
    allowed = frozenset(codes)

    def check(value: JsonValue) -> bool:
        return (value is None or isinstance(value, str)) and value in allowed

    return check


_ORDER_STATES = _one_of(
    None, "unpaid", "paid", "ingested", "awaiting_shipment", "shipped", "delivered", "cancelled",
    "refunded",
)  # fmt: skip

# One model per automation the worker runs (`WORKER_AUTOMATIONS`; a test keeps them in step).
OUTPUT_MODELS: Mapping[str, Mapping[str, Field]] = {
    ORDER_INTAKE: {
        "receipt_id": _id, "written": _flag, "state": _ORDER_STATES,
        "mode": _one_of("history", "reconcile", "sweep"), "orders_stored": _count,
        "receipts_read": _count, "drifts": _count, "etsy_receipts": _count,
        "skipped": _one_of("not_connected", "locked"),
    },
    ORDER_DEDUP: {
        "receipt_id": _id, "recorded": _count, "skipped": _one_of("not_connected"),
    },
    SALES_COUNTERS: {
        "receipt_id": _id, "rebuilt": _count, "moved": _count, "skipped": _one_of("not_stored"),
    },
    TRACKING_SYNC: {
        "receipts": _count, "recorded": _count, "via": _one_of("webhook", "schedule"),
        "skipped": _one_of("not_connected"),
    },
    INCIDENT_DETECTION: {
        "checked": _count, "opened": _count, "resolved": _count, "escalated": _count,
        "ship_by_alerts": _count, "mode": _one_of("reviews"), "reviews_read": _count,
        "low_review_alerts": _count, "truncated": _flag, "incident_id": _uuid,
    },
}  # fmt: skip


class TypedOutputs:
    """`OutputCheck` from `OUTPUT_MODELS`."""

    def __init__(self, models: Mapping[str, Mapping[str, Field]] = OUTPUT_MODELS) -> None:
        self._models = models

    def check(
        self, automation_id: str, output: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue] | None:
        model = self._models.get(automation_id)
        if model is None:
            return None
        kept = {key: value for key, value in output.items() if key in model and model[key](value)}
        dropped = len(output) - len(kept)
        if dropped:
            _log.warning(
                "automation.output_dropped",
                extra={"automation_id": automation_id, "dropped": dropped},
            )
        return kept or None


__all__ = ["OUTPUT_MODELS", "TypedOutputs"]
