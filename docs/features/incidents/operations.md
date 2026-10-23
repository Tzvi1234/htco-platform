# Incidents — Operations

## Reading incidents

Until the incident center (6.5): `select state, cause, severity, receipt_id, detected_at from app.incidents where state <> 'resolved' order by detected_at` (as the owner in the Supabase SQL editor), and `app.incident_events` for the history.

## Runbook

| Symptom | Check | Fix |
|---|---|---|
| An incident stays open though tracking is in Etsy | Has `tracking-sync` run for that receipt (`tracking_events`)? | Let the schedule run, or retry the order's dead-lettered execution |
| An incident must be closed by hand | It cannot be resolved without evidence, by design | Escalate it, or record the evidence that applies (the order cancelled, tracking present) |
| A duplicate incident | Impossible for one key (partial unique index); two causes on one order are two incidents | — |

## Rollback

Remove `fulfillment.inspect` from `HTCO_WORKER_SCHEDULES`: nothing new opens; incidents stay for review. The migration is additive; tables are kept.
