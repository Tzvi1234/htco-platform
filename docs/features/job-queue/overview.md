# Job queue and worker — Overview

| | |
|---|---|
| **Slug** | `job-queue` |
| **Phase / slices** | Phase 5 — slice 5.1 (queue, worker, schedules, maintenance jobs), slice 5.2 (inbox, outbox, `Idempotency-Key`); the durable automation engine (5.10) runs on it |
| **Requirements** | [FR-AUT-03](../../product/PRD.md) (retries, dead letters), [FR-SHOP-04](../../product/PRD.md) (purge after disconnect), ER-REL-01, ER-REL-05 |
| **Decision** | [ADR-0007](../../adr/0007-job-queue.md) (accepted 2026-10-22: in-house Postgres queue) |
| **Status** | Built and tested in CI; aligned with the unified execution plan (5.0: migration `20261023120000`, correlation and causation IDs); the `agent-worker` deployment is #54 (see [operations](operations.md#deployment)) |
| **Last updated** | 2026-10-22 |

## What and why

Background work (maintenance today; receipt ingest, outbox delivery and automation executions next) runs as **jobs** on a Postgres table that workers claim with a lease. A job runs at least once and its handler is idempotent, so a crash, a restart or a second worker never repeats a business effect. Jobs that keep failing end in a dead-letter state with their last error instead of looping. Recurring work is one job per period, enqueued by every worker but created once.

## Users and actors

- **System:** the `agent-worker` process (`python -m htco_worker`) runs the jobs; use cases enqueue them through the `JobQueue` port.
- **Operator / admins:** see dead-lettered jobs (`htco admin dead-letters`; the admin view arrives with slice 5.9).

## Acceptance criteria

| ID | Criterion (Given / When / Then) | Slice | Status |
|---|---|---|---|
| AC-5.1.1 | Given a worker running a job, when the process is killed (SIGKILL), then after a restart the job completes exactly once and its business effect happens once. | 5.1 | Pass in CI (real process killed) |
| AC-5.1.2 | Given two or more workers, when they claim at the same time, then no job is claimed twice and no two jobs with the same lock key (one per shop) run at once. | 5.1 | Pass in CI and in memory |
| AC-5.1.3 | Given a job that fails on every attempt, when its attempts are used up, then it is `dead` with its last error (never buyer data), listed for its organization's admins. | 5.1 | Pass |
| AC-5.1.4 | Given a worker running a job, when it receives SIGTERM, then it claims nothing new, lets the job finish within the grace period and exits 0; a job past the grace period is left to its lease. | 5.1 | Pass in CI (real process signalled) |
| AC-5.1.5 | ADR-0007 is Accepted with the spike results. | 5.1 | Done |
| AC-5.1.6 | Given two worker instances, when a schedule's period comes, then its job runs once; the purge deletes only the disconnected shop's data and only after its purge date. | 5.1 | Pass in CI and in memory |
| AC-5.2.1 | Given an external event, when it is recorded twice (also at the same time), then there is one inbox row and one job. | 5.2 | Pass in CI |
| AC-5.2.2 | Given a business change committed with its outbox message, when the worker crashes before delivering, then after restart the effect is delivered exactly once. | 5.2 | Pass in CI (real process killed) |
| AC-5.2.3 | Given a POST with an `Idempotency-Key`, when it is repeated with the same body, then the stored response comes back with no second effect; the same key with another body is refused (422). | 5.2 | Pass (API and Postgres store) |

## Scope

- **In (5.1):** `JobQueue` port and Postgres adapter; the worker loop (claim and lease, heartbeat, retries with backoff and jitter, dead letter, per-resource locks, graceful SIGTERM, scheduled jobs); `jobs.jobs` and `jobs.worker_heartbeats`; the `htco_worker` database role; the health port; maintenance schedules: daily capability probes (GET-only), refresh-token expiry checks, purge of disconnected shops after their purge date, pruning of finished jobs; `htco admin dead-letters`; the app-spec change for the `agent-worker` component (separate, unmerged pull request until the owner approves the cost).
- **In (5.2):** `jobs.inbox` (unique `(source, event_id)`, recorded with its job in one transaction), `jobs.outbox` (unique idempotency key, status, attempts, next attempt, written with its `outbox.deliver` job inside the business transaction), the delivery job, `private.idempotency_keys` and `run_idempotently` for unsafe routes (first used by `POST /v1/automations/{id}/executions` in slice 5.10).
- **Out:** webhook ingress (5.3; every Etsy event type, only if the Seller App offers webhooks), any outbox destination (HTCO writes nothing to Etsy; email arrives in Phase 6), receipt ingest and reconciliation (5.4, 5.5), queue metrics, alerts and the admin view with retry (5.9).

## External facts learned

- Etsy: the daily probes are the slice 4.5 probes (three GETs per shop: shop, one receipt, one listing). A token refresh on the way is a POST to Etsy's OAuth token endpoint, not to the shop API, exactly as in the API process; the adapter's guard refuses any other non-GET request (ADR-0008).

## Known limits

- One job at a time per worker process; more throughput means more processes.
- Workers poll every second (no `LISTEN/NOTIFY`); enqueue-to-start latency is up to one poll interval.
- A schedule enqueues only the current period: periods missed while no worker ran are skipped, not caught up.
- The purge deletes the synced data that exists today (probe results). The orders, items and shipments of slices 5.4 and 5.5 join the purge list in those slices, minus what legal retention keeps.
- The Etsy rate-limit budget is still per process (slice 4.1); the worker and the API each see their own view until a shared budget is needed.
