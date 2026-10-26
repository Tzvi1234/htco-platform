# Fulfillment monitoring — Operations

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `HTCO_WORKER_SCHEDULES` contains `fulfillment.inspect` | absent (off) | The 15-minute inspection per connected shop. Removing it stops the schedule only: once Tracking Synchronization is active, `order.shipped` webhooks still start it. Setting both automations' `readiness` back to `draft` in `registry/automations/`, merging it and running the registry sync (the Migrate workflow's `htco registry sync --automations --apply`: the worker reads the definitions from the database) makes the engine refuse every new trigger; executions already pending or retry-scheduled still run, so stop the agent-worker deployment as well when they must stop at once (the queue resumes when it starts again). Incidents stay for review |
| `HTCO_WORKER_SCHEDULES` contains `fulfillment.reviews` | absent (off) | The hourly low-review read per connected shop (AC-6.9.2, `etsy.reviews.read`, GET). Removing it stops the reads and new low-review alerts; existing alerts stay. The full stop is the automation's readiness: Incident Detection & Escalation stays `draft` until 6.7, and the engine refuses it whatever the flag |
| `app.sla_policies` rows | none | No row: 7 business days from payment to the first tracking (the owner's decision of 2026-10-23). Add a row per shop, supplier or shipping method once the owner agrees the windows |

Tracking Synchronization is deployed in the worker only when the Etsy app and the token vault are configured; Incident Detection always.

## Activation

Both automations are `draft`; the engine refuses them and the worker logs `fulfillment.not_started` or `fulfillment.tracking_not_started` (warning, once per trigger). The owner activates them in slice 6.7.

## Monitoring

- Logs: `fulfillment.inspections_enqueued` (count), `fulfillment.inspection` (which automations started).
- Executions: the automation engine's executions and dead letters (slice 5.10); Admin › Health (slice 5.9) shows each automation's executions, success rate, retries, dead letters and latency, and retries a dead letter (audited).

## Runbook

| Symptom | Check | Fix |
|---|---|---|
| A real late order has no incident | Is `fulfillment.inspect` in `HTCO_WORKER_SCHEDULES`? Is the automation `ready`? Does the order have a tracking event? | Enable the schedule; activate (6.7); a tracking event means it is not late |
| Too many incidents | The policy windows | Raise `window_days` or switch to business days for that shop or supplier |
| Tracking not recorded | Executions of `tracking-sync` dead-lettered? The gateway's `etsy:api` breaker? | Retry the execution from the dead-letter CLI once Etsy answers |
