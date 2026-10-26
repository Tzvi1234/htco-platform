"""The automations whose handlers the worker deploys (slices 5.4, 5.11, 6.1, 6.2): one list that
both processes read, so they can never disagree (#157).

The worker runs them (`htco_worker.main`: `intake_handlers` and `fulfillment_handlers`; a test
checks that its handler keys are exactly this set). The API only creates their executions: a
user's trigger (the counters rebuild, an audited admin retry) passes the engine's "deployed"
check there and becomes a job for the worker. Each still runs only once the owner made it ready.
"""

from htco_application.etsy_webhooks import ORDER_INTAKE
from htco_application.fulfillment_schedule import INCIDENT_DETECTION, TRACKING_SYNC
from htco_application.order_automations import ORDER_DEDUP, SALES_COUNTERS

WORKER_AUTOMATIONS = frozenset(
    {ORDER_INTAKE, ORDER_DEDUP, SALES_COUNTERS, TRACKING_SYNC, INCIDENT_DETECTION}
)

__all__ = ["WORKER_AUTOMATIONS"]
