"""The outbox message: an effect written in the business transaction, delivered once later."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from htco_application.kernel.types import JsonValue
from htco_domain.jobs import check_job_kind

MAX_OUTBOX_KEY_LENGTH = 200


@dataclass(frozen=True)
class OutboxMessage:
    organization_id: uuid.UUID
    destination: str  # `<system>.<effect>`, the deliverer that sends it
    idempotency_key: str
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    shop_id: uuid.UUID | None = None
    # The gateway's action this effect belongs to (ADR-0019): when the delivery succeeds or dies,
    # the action moves to VERIFYING or FAILED (slice 5.12). By convention an Etsy write's
    # `destination` is its action name, for example `etsy.inventory.update`.
    action_execution_id: uuid.UUID | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        check_job_kind(self.destination)
        if self.correlation_id is not None and not (
            0 < len(self.correlation_id) <= MAX_OUTBOX_KEY_LENGTH
        ):
            raise ValueError("a correlation ID is 1 to 200 characters")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > MAX_OUTBOX_KEY_LENGTH:
            raise ValueError("an outbox message needs an idempotency key of 1 to 200 characters")
