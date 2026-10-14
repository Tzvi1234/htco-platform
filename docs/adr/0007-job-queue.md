# ADR-0007: Postgres-native job queue behind a port

- **Status:** Proposed; implementation chosen by a spike at the start of Phase 4
- **Date:** 2026-10-14
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

## Proposed decision

Define a `JobQueue` port now (enqueue with idempotency key, claim with lease, complete, fail with retry policy, dead-letter, schedule). Choose the implementation with a time-boxed spike at the start of Phase 4 against these criteria: crash safety (kill-worker test), duplicate suppression, dead-letter handling, scheduling, per-shop locks, observability (queue depth and age), and operational simplicity. The current preference is option 2 or 3 because of pgmq's alpha status and missing dead-letter support.

## Consequences

- Positive: the application layer is independent of the choice; switching later is contained.
- Negative: a spike delays the first worker by a day or two.

## Validation

The Phase 4 spike result is recorded in the validation log below and the status changes to Accepted with the chosen option.

## Validation log

- 2026-10-14 — The automation engine ([ADR-0013](0013-automation-engine.md)) is the queue's main consumer; its `ExecutionStore` port moves onto the queue in Phase 4.
