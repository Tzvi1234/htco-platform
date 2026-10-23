"""HTCO agent-worker and its composition root (slice 5.1, ADR-0007).

The worker consumes the job queue: maintenance schedules now, the automation engine's executions
and outbox deliveries from slices 5.2 and 5.10. No business rules live in this package.
"""
