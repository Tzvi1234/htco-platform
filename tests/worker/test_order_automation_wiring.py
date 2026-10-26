"""The worker deploys the three order automations' handlers and lets order events start them
(slice 5.11); the engine still refuses each until the owner activates it."""

from htco_application.automation.deployed import WORKER_AUTOMATIONS
from htco_application.etsy_webhooks import ORDER_INTAKE
from htco_application.order_automations import (
    COUNTED_EVENTS,
    ORDER_DEDUP,
    SALES_COUNTERS,
    VERIFIED_EVENTS,
    OrderEventTriggers,
)
from htco_persistence.database import Database
from htco_worker.main import (
    fulfillment_handlers,
    intake_handlers,
    order_event_subscribers,
    worker_engine,
)

DATABASE = "postgresql://htco@db.invalid/htco"


def test_intake_handlers__the_three_order_automations() -> None:
    handlers = intake_handlers(Database(DATABASE), object())  # type: ignore[arg-type]

    assert set(handlers) == {ORDER_INTAKE, ORDER_DEDUP, SALES_COUNTERS}


def test_order_events__subscribed_to_start_the_order_automations() -> None:
    subscribers = order_event_subscribers(worker_engine(Database(DATABASE)))

    assert set(subscribers) == VERIFIED_EVENTS | COUNTED_EVENTS
    assert all(isinstance(s, OrderEventTriggers) for s in subscribers.values())


def test_worker_automations__exactly_the_handlers_the_worker_deploys() -> None:
    """The API accepts triggers for these (and only these) and leaves the running to the worker:
    one shared list, so the two processes can never disagree (#157)."""
    deployed = set(intake_handlers(Database(DATABASE), object()))  # type: ignore[arg-type]
    deployed |= set(fulfillment_handlers(Database(DATABASE), object()))  # type: ignore[arg-type]

    assert deployed == WORKER_AUTOMATIONS
