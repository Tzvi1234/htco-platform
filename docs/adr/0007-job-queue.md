# ADR-0007: Postgres-native job queue behind a port

- **Status:** Accepted (2026-10-22, after the Phase 5 spike): option 3, a small in-house queue; amended 2026-10-23 by [ADR-0019](0019-unified-execution-engine-and-action-gateway.md) (the only transport of the execution engine; Postgres advisory locks for browser profiles; Valkey optional)
- **Date:** 2026-10-14 (proposed), 2026-10-22 (accepted)
- **Deciders:** Owner, engineering

## Context

Ingest, sync, inspections, outbox delivery and agent runs need durable, at-least-once background execution with retries, a dead-letter path, scheduling and per-resource serialization (for example one sync per shop at a time). The original planning brief names Supabase Queues (pgmq), conditional on a proof of concept.

## Facts

- Supabase Queues (pgmq) is labeled **public alpha** as of 2026-10-14. It offers send, read with visibility timeout, archive and delete, but no built-in dead-letter queue, priorities or tenant columns.
- A plain table claimed with `FOR UPDATE SKIP LOCKED` is a proven pattern and lets jobs carry `organization_id`, `shop_id`, priority and status that are easy to query.
- Mature Python libraries implement that pattern on Postgres (for example `procrastinate`: retries, periodic tasks, queueing locks, `LISTEN/NOTIFY`).
- `LISTEN/NOTIFY` and advisory locks need a direct or session-mode connection (see ADR-0005).

## Options considered

1. **Supabase Queues (pgmq)** with our own DLQ and retry logic.
2. **An existing Postgres task library** such as `procrastinate`.
3. **A small in-house queue** on one `jobs` table with `SKIP LOCKED`, leases, attempts and a dead state.

## Decision

Option 3: one `jobs.jobs` table, claimed with `FOR UPDATE SKIP LOCKED`, behind the `JobQueue` port (`htco_application.jobs`), with its only adapter in `htco_persistence.jobs` and the worker loop in `workers/runner` (`htco_worker`). The design is in [`docs/features/job-queue/design.md`](../features/job-queue/design.md).

## Spike (2026-10-22, time-boxed to the first day of Phase 5)

The spike compared the three options against the criteria below. Options 1 and 2 were assessed from their documentation and from what our constraints (ADR-0005, ADR-0011) require; option 3 was built as the candidate and proven in CI against Postgres 17 (the `database` job). No new dependency was added for the spike.

| Criterion | 1. pgmq | 2. procrastinate | 3. In-house table |
|---|---|---|---|
| Crash safety (kill-worker test) | Visibility timeout re-delivers | Stalled jobs can be found and retried (documented API) | Lease plus heartbeat; an expired lease is released and counts as an attempt. Proven by killing a worker process with SIGKILL mid-job (`test_worker_killed_mid_job__the_job_completes_exactly_once_after_restart`) |
| Duplicate suppression | None built in | Queueing locks (one waiting job per lock) | `unique (kind, idempotency_key)`: enqueueing twice returns the first job |
| Dead-letter handling | Not built in (alpha) | `failed` status | `dead` state with the last error, listed per organization (`htco admin dead-letters`) |
| Scheduling | Not built in (`pg_cron` beside it) | Periodic tasks, deduplicated | One job per period, keyed by the period, so any number of workers enqueue it once |
| Per-shop locks | Not built in | Locks per task | Partial unique index: at most one running job per `lock_key`, enforced by Postgres |
| Tenancy | No tenant columns | No tenant columns; its own schema | `organization_id`, `shop_id` with foreign keys; the worker runs each job inside its organization's RLS scope |
| Same transaction as business data | Possible (SQL functions) | Its own connector; enqueueing inside our psycopg transaction is not its main path | `enqueue_in(connection, spec)`: the job commits with the inbox row or the business change (the outbox pattern of slice 5.2) |
| Schema source (ADR-0011) | Extension | Ships and migrates its own schema, a second schema source | Our migrations and pgTAP tests |
| Observability | Queue metrics functions | Its own tables | Plain columns: depth, age, attempts, dead count, heartbeats |
| Operational simplicity | Alpha; needs our DLQ and retries | A new dependency and its upgrade path; asynchronous driver (psycopg's async driver cannot run on Windows' default event loop, see `htco_persistence.database`) | About 300 lines of adapter and 250 of loop, all tested; no new dependency |

**Result:** option 3 meets every criterion, keeps one schema source and lets a job share the business transaction, which the inbox and outbox (slice 5.2) and the durable automation engine (slice 5.10) need. pgmq stays out until it leaves alpha and offers a dead-letter path; procrastinate would bring a second schema source and its own connector for features we now have. The port keeps a later switch contained.

## Consequences

- Positive: the application layer is independent of the choice; switching later is contained. Jobs are tenant-aware rows the worker role reaches through RLS.
- Negative: we own the queue's code (claim, lease, retry, dead letter, scheduling, pruning) and its tests. Workers poll (every second by default) instead of `LISTEN/NOTIFY`; at pilot volume a poll is one indexed query.

## Validation

The Phase 5 spike result is recorded above and in the validation log; the status is Accepted with option 3.

## Validation log

- 2026-10-14 — The automation engine ([ADR-0013](0013-automation-engine.md)) is the queue's main consumer; its `ExecutionStore` port moves onto the queue in Phase 5.
- 2026-10-22 — Spike complete; option 3 accepted. AC-5.1.1 to AC-5.1.6 covered by tests against Postgres in CI (`tests/integration/test_job_queue.py`, `tests/integration/test_worker_process.py`, `supabase/tests/database/10_job_queue.test.sql`) and by the in-memory worker tests (`tests/worker/`).
- 2026-10-23 — Amended by ADR-0019: the queue is the single transport for every execution channel (API, Browser, Internal, Human resumption). Browser profiles are serialized by Postgres advisory locks held for a session, not by `lock_key` alone. A Postgres token bucket backs the cross-worker Etsy rate limit; Valkey is an optional backend for the limiter, added only on measured need.
