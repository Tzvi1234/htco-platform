# Phase 5 — Unified Automation Foundation (engine, Action Gateway, orders)

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L (was L; three contract slices are added, and three slices already exist in open pull requests) |
| **Depends on** | Phase 4 |
| **Requirements** | FR-AUT-01, FR-AUT-02, FR-AUT-03, FR-AUT-04, FR-AUT-05, FR-FIN-01, FR-ORD-01, FR-ORD-02, FR-ORD-03, FR-ORD-04, FR-ORD-05, FR-ORD-08, FR-ORD-09, FR-SHOP-04, FR-TASK-03 |
| **Decisions** | [ADR-0007](../../adr/0007-job-queue.md), [ADR-0013](../../adr/0013-automation-engine.md), [ADR-0018](https://github.com/Tzvi1234/htco-platform/pull/53) (order history, in PR #53), [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md), [ADR-0020](../../adr/0020-per-shop-browser-environment.md), [ADR-0021](../../adr/0021-authenticator-service.md), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) |
| **Design** | [execution-engine.md](../../architecture/execution-engine.md) |
| **Status** | PARTIAL: 5.1, 5.2 and 5.10 in open PRs #53, #55 and #58 (not merged; aligned first, see below); the order history store merged ahead of the phase (#57) |

The phase was renamed on 2026-10-23 from "Automation engine and orders CRM" (owner's architecture upgrade, [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md)). The file name stays the same so links keep working.

## Progress

- [ ] 5.0 Alignment of PRs #53, #55, #58 with the contracts (blocking their merge)
- [x] 5.1 Job queue and worker (PR #53; aligned: migration `20261023120000_job_queue.sql` because a shop fix took `20261023090000`, pgTAP `10_job_queue`, `correlation_id` and `causation_id` on jobs; deployment in #54)
- [x] 5.2 Inbox and outbox (PR #55; aligned: migration `20261023130000_inbox_outbox.sql`, pgTAP `11_inbox_outbox`, `action_execution_id` and `correlation_id` on outbox messages)
- [ ] 5.3 Webhook ingress for all 11 Etsy events
- [ ] 5.4 Receipt ingest on the order history store (#57)
- [ ] 5.5 Reconciliation and backfill on the queue
- [ ] 5.6 Orders API
- [ ] 5.7 Orders UI
- [ ] 5.8 Drills
- [ ] 5.9 Queue, execution and sync health (Prometheus metrics)
- [ ] 5.10 Durable automation engine (PR #58)
- [ ] 5.11 Order automations activated
- [ ] 5.12 Action Gateway and the generic execution contract
- [ ] 5.13 Execution Router and channel adapters (API real, Browser interface and fake, Internal, Human contract)
- [ ] 5.14 Shared approval contract (in-memory; Postgres in Phase 7)
- [ ] 5.15 Circuit breaker, rate and concurrency control, idempotency for actions
- [ ] 5.16 Shop Environment Registry: data model, statuses, state machine (contracts only)
- [ ] 5.17 Secret Broker and Authenticator contracts
- [ ] 5.18 Status management for tasks, executions and environments

**Order of work** ([EXECUTION_ORDER](../EXECUTION_ORDER.md)):

1. 5.0 aligns the open PRs. Then 5.1 → 5.2 → 5.10 merge in that order.
2. 5.12, 5.15 and 5.13 follow, because slices 5.3 to 5.5 run as automations whose business steps call the gateway.
3. 5.14, 5.16, 5.17 and 5.18 are contract slices and can run in parallel with 5.3 to 5.7.
4. 5.11 activates the three order automations last.

**Foundation already in code (main).**

- The automation model and an in-memory engine: `htco_domain.automation` and `htco_application.automation.engine`, tested in `tests/application/test_automation_engine.py`.
- The tool registry and access rules (`htco_domain.tooling`, `decide_tool_access`).
- Etsy's read-only adapter and token vault (Phase 4).
- **The order history store** (#57): `app.etsy_orders`, `etsy_order_items`, `etsy_order_refunds`, `etsy_order_buyers` and `etsy_order_sync_state`. It provides a one-time resumable backfill and a 30-day re-read, started in the API process under a per-shop advisory lock.

## Goal

1. Every paid Etsy order appears in HTCO exactly once, with its items, current state, source and freshness, whether Etsy's webhook arrived, arrived twice, arrived late or never arrived.
2. All of it runs on **one shared execution infrastructure** that every later automation and AI employee uses:
   - the job queue, inbox and outbox;
   - the durable automation engine;
   - an **Action Gateway** with a generic tool execution contract;
   - an **Execution Router** with four channels (API, Browser, Internal, Human);
   - a shared approval contract;
   - circuit breakers, rate and concurrency limits, and idempotency;
   - status management for tasks, executions and browser environments.
3. The browser engine itself is not built here. Its interfaces, data models and contracts are, with a fake adapter, so Phases 8A and 8B plug in without changing the engine.

Order work is done by three deterministic automations (Etsy Order Intake, Order Deduplication & Verification, Product Sales Counters), with no model calls and no LangGraph.

## Scope

**In:**

- **Kept:**
  - the job queue and the `agent-worker` (#53);
  - inbox, outbox and `Idempotency-Key` (#55);
  - the durable engine and registry sync (#58);
  - the order history store (#57), moved onto the queue.
- **Ingest:**
  - webhook ingress for all 11 Etsy webhook events;
  - receipt ingest into the order history store;
  - reconciliation, backfill and the nightly sweep;
  - the order state machine and status events;
  - basic financial entries;
  - the orders API and UI;
  - drills;
  - Prometheus metrics for queues, executions and sync.
- **New:**
  - the Action Gateway and execution contract (`packages/contracts/actions/`);
  - the Execution Router;
  - the API channel adapter (real) and the Internal channel;
  - the Browser channel interface with `FakeBrowserChannel`;
  - the Human channel contract;
  - the approval contract with an in-memory implementation;
  - PyBreaker, aiolimiter with a Postgres token bucket, concurrency slots and `action_executions`;
  - the Shop Environment Registry tables and state machine (no browser runs);
  - the Secret Broker and Authenticator Protocols, with fakes;
  - status management.

**Out:**

- tracking and incidents (Phase 6);
- the Postgres approvals and activity store (Phase 7);
- employees through the tool bridge (Phase 8);
- the real browser engine, Browser Use and the Browser Controller (Phase 8A);
- PyOTP, enrolment and code display (Phase 8B);
- any Etsy **write** (the first owner-triggered writes are in Phases 6 and 7).

## Alignment of the open pull requests

Owner's instruction (2026-10-23): **do not merge #53, #55 or #58 before they are aligned with this plan.** The list below is the complete change set. Anything not listed is kept as is. No infrastructure is duplicated: the gateway is a new layer **on top of** the queue, inbox, outbox and engine, not a replacement.

### Common to all three

| Change | Why |
|---|---|
| **Renumber the migrations after merged #57's `20261022120000_etsy_order_history.sql`.** #53 `20261022090000_job_queue.sql`, #55 `20261022100000_inbox_outbox.sql` and #58 `20261022110000_automation_engine.sql` become, for example, `20261023090000`, `20261023100000` and `20261023110000`, keeping their order. | Supabase's `db push` refuses (or, with `--include-all`, applies out of order) a migration older than the last applied one; production already has `20261022120000` |
| **Renumber the pgTAP files.** #53's `08_job_queue.test.sql` collides with merged `08_etsy_order_history.test.sql`. Use `10_job_queue`, `11_inbox_outbox` (from #55's `09`) and `11_automation_engine` (from #58's `10`). | Deterministic test order and no confusing duplicates |
| Rebase onto the plan PR. Conflicts in `docs/plan/PLAN.md`, this file, `DATA_MODEL.md`, `EVENT_CATALOG.md` and `ADR-0007` resolve by keeping this plan's text and adding each PR's progress tick and validation-log line | The plan PR rewrites these files |

### PR #53 — slice 5.1 (job queue, worker, maintenance)

| | Items |
|---|---|
| **Kept as is** | `JobQueue` port and the `jobs.jobs` table (unique `(kind, idempotency_key)`, leases, heartbeat, backoff with jitter, `dead` state, `lock_key` partial unique index, release of expired leases, pruning); `htco_worker` (claim, run and complete loop, SIGTERM grace, schedules, `/healthz`); the `htco_worker` database role with RLS per organization; maintenance schedules (capability probes, refresh-token expiry, purge, prune); `htco admin dead-letters`; ADR-0007 accepted; ADR-0018 |
| **Must change** | 1. Add optional `correlation_id` and `causation_id` to `JobSpec`, `ClaimedJob` and `jobs.jobs` (additive columns). The gateway propagates them from trigger to job to action to outbox (ADR-0019; the event envelope rules). 2. The migration and pgTAP renumbering above. 3. Docs: the "Not deployed" note refers to #54 *and* to the `browser-worker`, which is a separate component in Phase 8A, not in `agent-worker` |
| **Follow-up, not blocking** | The capability-probe job calls Etsy directly through the guarded adapter. After 5.12 it calls the `etsy.platform.health` action through the gateway (API channel), with the same GET-only behaviour |
| **Missing (added by later slices, not by #53)** | The shared rate limit across workers (5.15), circuit breakers (5.15), and Prometheus metrics (5.9) |

### PR #55 — slice 5.2 (inbox, outbox, `Idempotency-Key`)

| | Items |
|---|---|
| **Kept as is** | `jobs.inbox` unique `(source, event_id)` with its job in one transaction; `jobs.outbox` with `pending`, `delivered` and `dead`, attempts and next attempt; `add_outbox_in`; the `outbox.deliver` job and `Deliverer` port; `private.idempotency_keys` and `run_idempotently` for unsafe HTTP routes |
| **Must change** | 1. Add optional `action_execution_id` and `correlation_id` to `OutboxMessage` and `jobs.outbox` (additive). When delivery succeeds or dies, the gateway's `action_executions` row moves to `VERIFYING` or `FAILED`, so a write is verified after delivery, not before. 2. Convention, no code change: the outbox `destination` of an Etsy write equals the action name (for example `etsy.inventory.update`). The API channel registers one `Deliverer` per write action in 5.13. 3. The renumbering above |
| **No duplication** | The HTTP `Idempotency-Key` stays the HTTP layer's store. A route that submits an action derives `ActionRequest.idempotency_key` from it. `action_executions` is the gateway's own key space (one row per business effect), not a second HTTP idempotency store |
| **Missing** | Nothing for 5.2. Webhook signature verification is 5.3 |

### PR #58 — slice 5.10 (durable automation engine)

| | Items |
|---|---|
| **Kept as is** | `DurableAutomationEngine` (`trigger`, `execute`, crash recovery, admin retry); the shared `authorize` gate; `app.automation_definitions`, `automation_executions` and `automation_execution_events` with the transition trigger and RLS; `htco registry sync --automations` and its CI check; the API routes; `user_capability`; the `HTCO_AUTOMATIONS_ENABLED` flag |
| **Must change** | 1. **Handler context:** `Handler` receives a `HandlerContext` (execution ID, automation ID, actor, organization, shop, correlation and causation IDs, and an `ActionGateway` client) instead of the bare payload. Business effects go through `gateway.submit(...)`, not straight to use cases or the outbox. The in-memory engine keeps a compatible adapter, so existing tests keep passing. 2. **Waiting state:** add `WAITING` to `ExecutionState`, with `RUNNING → WAITING` (an action returned `PENDING_APPROVAL` or `HANDED_TO_HUMAN`), `WAITING → RETRY_SCHEDULED` (resumed by `approvals.decided.v1` or the task's completion) and `WAITING → CANCELLED` (rejected or expired). The job finishes while waiting, so no lease is held for days (domain machine, database check and trigger, tests). 3. The renumbering above |
| **Missing** | `action_executions` and the gateway (5.12); the resume trigger from approvals (5.14 contract, Phase 7 events) |

### PR #54 — `agent-worker` deployment (stacked on #53)

Kept as is: one $5 component, with the budget raised to $10. It is the worker for the automation, API and Internal channels. The browser worker is **not** added to it ([Phase 8A](phase-08a-browser-infrastructure.md), separate cost line).

### Merged #57 — order history store

Kept: the tables, upsert by `(shop_id, receipt_id)`, the resumable backfill checkpoint, the 30-day re-read, buyer data in its own table, and no contact data. Slices 5.4 and 5.5 change only **who triggers it**: the in-process API background task is replaced by jobs (Etsy Order Intake's schedule, webhook and dashboard "stale" triggers enqueue a job), and the advisory lock is replaced by the job's `lock_key` `shop:{shop_id}:orders`. **`app.etsy_orders` is the order store.** No second `orders` table is created. `order_status_events`, `sync_drifts` and `financial_entries` reference `etsy_orders` ([DATA_MODEL](../../architecture/DATA_MODEL.md)).

## Automations activated

| Automation | Registry | Slices | Business owner |
|---|---|---|---|
| Etsy Order Intake | [`etsy-order-intake.toml`](../../../registry/automations/etsy-order-intake.toml) | 5.3, 5.4, 5.5 | Eitan (the owner is accountable until Eitan is active in Phase 9) |
| Order Deduplication & Verification | [`order-dedup-verification.toml`](../../../registry/automations/order-dedup-verification.toml) | 5.4, 5.5 | Eitan (as above) |
| Product Sales Counters | [`product-sales-counters.toml`](../../../registry/automations/product-sales-counters.toml) | 5.4 | Roni (the owner is accountable until Roni is active in Phase 13) |

An automation is activated only when all of these hold:

- all 18 sections of its specification are `defined`;
- its input and output schemas are in `packages/contracts/automations/`;
- its replay, failure and recovery tests pass;
- its runbook exists;
- the owner approves the activation (ADR-0012 rule 2).

## Owner prerequisites

- Webhook endpoint registered in the Etsy developer portal (webhooks work for personal apps since 2026-02-17), with the signing secret stored in the platform's secrets.
- Owner review of the Phase 5 alignment list above before #53, #55 and #58 merge.

## Slices

### 5.0 — Alignment of PRs #53, #55, #58

The changes listed in [Alignment of the open pull requests](#alignment-of-the-open-pull-requests), made in each PR. The changes are additive, and no AC of 5.1, 5.2 or 5.10 changes.

| ID | Acceptance criterion |
|---|---|
| AC-5.0.1 | `supabase db reset` applies `20261022120000_etsy_order_history.sql` before the job queue, inbox, outbox and engine migrations, and a migration-order test fails if a new migration is older than the newest on `main`. |
| AC-5.0.2 | `JobSpec`, `jobs.jobs` and `jobs.outbox` carry `correlation_id` (and `causation_id` for jobs), and a test traces one ID from trigger to execution to job to outbox. |
| AC-5.0.3 | `ExecutionState.WAITING` exists, with only the transitions listed above (state-machine tests, plus a database trigger refusing the others). |
| AC-5.0.4 | Handlers receive a `HandlerContext`, and the in-memory engine's existing tests pass unchanged. |

### 5.1 — Job queue and worker (PR #53)

As implemented in #53. Time-boxed spike recorded in ADR-0007. Includes the `JobQueue` port and adapter; the worker with claim and lease, heartbeat, retries with backoff and jitter, the dead-letter state, per-resource locks, graceful shutdown and scheduled jobs; the `agent-worker` with a health port; `worker_heartbeats`; and the scheduled maintenance jobs.

| ID | Acceptance criterion |
|---|---|
| AC-5.1.1 | Killing the worker mid-job leads to exactly one successful completion after restart (automated kill test). |
| AC-5.1.2 | Two workers never run the same job or the same per-shop lock concurrently. |
| AC-5.1.3 | A job that fails N times lands in the dead-letter state with its last error, visible to admins. |
| AC-5.1.4 | SIGTERM lets the current job finish (within a deadline) before exit. |
| AC-5.1.5 | ADR-0007 updated to Accepted with the spike results. |
| AC-5.1.6 | Scheduled maintenance jobs run once per period even with two worker instances; the purge deletes only the disconnected shop's data and only after its purge date. |

### 5.2 — Inbox and outbox (PR #55)

As implemented in #55: the `inbox` unique by `(source, event_id)`; the `outbox` unique by idempotency key, with delivery status, attempts and next attempt time; the delivery job; and `Idempotency-Key` handling for unsafe API requests.

| ID | Acceptance criterion |
|---|---|
| AC-5.2.1 | Inserting the same inbox event twice results in one row and one job. |
| AC-5.2.2 | A crash after the business commit but before delivery results in exactly one delivery after restart. |
| AC-5.2.3 | Repeating a POST with the same `Idempotency-Key` returns the stored response with no second effect; the same key with a different body is rejected. |

### 5.3 — Webhook ingress for all 11 Etsy events

`POST /webhooks/etsy`:

- verify the Standard Webhooks signature (`webhook-id`, `webhook-timestamp`, `webhook-signature`, HMAC-SHA256) on the raw body, with a 5-minute tolerance and a body size limit;
- store the event in the inbox (`source = "etsy"`, `event_id = webhook-id`);
- enqueue the routed job;
- respond 2xx within a second.

Reconciliation stays on whatever the webhooks do. Each event is routed as follows:

| Etsy event | Routed to (Phase 5) | Later consumers |
|---|---|---|
| `order.paid` | Etsy Order Intake: fetch the receipt, upsert, `orders.ingested.v1` | Product Sales Counters, ship-by SLA (6), notifications |
| `order.canceled` | Etsy Order Intake: `orders.cancelled.v1` | Counters, supplier stop (13), incidents (close) |
| `order.shipped` | Etsy Order Intake: shipments, `orders.updated.v1` | Tracking Synchronization (6) |
| `order.delivered` | Etsy Order Intake: delivered state, `orders.updated.v1` | Ship time per country (6), review request timing (feature backlog) |
| `listing.created` | Listing cache refresh (Internal), `catalog.listing_changed.v1` | Listing Health (11), Product Studio verification (12) |
| `listing.updated` | As above, with the changed fields | Listing Health: unintended edits (11) |
| `listing.deleted` | As above, marking the listing deleted | Catalog (12) |
| `inventory.created` | Inventory snapshot refresh, `catalog.inventory_changed.v1` | Price and stock sync (13) |
| `inventory.updated` | As above | Price monitoring (13) |
| `inventory.deleted` | As above | Catalog (12) |
| `inventory.out_of_stock` | As above, plus `catalog.out_of_stock.v1` | Out-of-stock alert (6, notifications) |

An unknown event type is stored, counted and not processed. If webhooks are unavailable for our app, the slice ships disabled behind a flag.

| ID | Acceptance criterion |
|---|---|
| AC-5.3.1 | Invalid signature, modified body, timestamp outside tolerance and replayed `webhook-id` are rejected or ignored as documented, and logged without the body. |
| AC-5.3.2 | p95 response time under 1 s with 20 concurrent valid webhooks. |
| AC-5.3.3 | Requests above the per-source rate limit get 429 and are logged; Etsy's retries are accepted after the window. |
| AC-5.3.4 | Each of the 11 event types is routed as in the table (table-driven test with recorded payloads); an unknown type is stored with no job. |

### 5.4 — Receipt ingest on the order history store

Etsy Order Intake fetches the receipt through the gateway (`etsy.receipts.read`, API channel) and upserts it into the #57 store with the existing `(shop_id, receipt_id)` upsert, in one transaction. It applies the update only if Etsy's `updated_timestamp` is newer. It records `order_status_events`, stores the raw payload hash, creates basic `financial_entries` (revenue, Etsy fees) labelled `ESTIMATED`, and emits `orders.ingested.v1` or `orders.updated.v1`. No contact data is read or stored (ADR-0018).

| ID | Acceptance criterion |
|---|---|
| AC-5.4.1 | The same receipt event processed 10 times yields one order, correct item quantities and one `orders.ingested.v1`. |
| AC-5.4.2 | An older update arriving after a newer one does not overwrite newer data. |
| AC-5.4.3 | Units sold and order counts are computed separately and exclude cancelled and refunded orders (property tests). |
| AC-5.4.4 | All money uses `Decimal` with currency; a float anywhere in the money path fails a test. |
| AC-5.4.5 | Every allowed order transition (FR-ORD-03) is tested, each illegal transition is rejected, and every transition records its source and `observed_at`. |
| AC-5.4.6 | Webhook ingest and the #57 sync write through the same upsert; no second order table exists (schema inventory test). A receipt fixture with fake contact values stores none of them (ADR-0018). |

### 5.5 — Reconciliation and backfill on the queue

The #57 backfill and 30-day re-read become jobs of Etsy Order Intake:

- a schedule per shop (every 10 minutes, or as the rate budget allows);
- a nightly 30-day sweep;
- a "stale on open" trigger from the dashboard, which enqueues and never runs in the API process;
- the Owner's manual sync.

`sync_cursors` (the #57 `etsy_order_sync_state` checkpoint is kept, and the cursor is added), a freshness metric per shop, and the job's `lock_key` `shop:{shop_id}:orders` replaces the API process's advisory lock.

| ID | Acceptance criterion |
|---|---|
| AC-5.5.1 | With webhooks disabled, every receipt in a 200-receipt fixture is ingested by reconciliation alone. |
| AC-5.5.2 | A receipt whose webhook was lost is ingested within one reconciliation interval. |
| AC-5.5.3 | The daily Etsy call count for all connected shops (backfill included) stays inside the app's rate-limit budget, computed in a test from the schedule and enforced by the 5.15 limiter. |
| AC-5.5.4 | A difference between a stored order and Etsy (status, items or shipments) is recorded in `sync_drifts` with both values and emits `orders.drift_detected.v1`; from Phase 6 it opens an incident (FR-ORD-09). |
| AC-5.5.5 | No order sync runs in the API process any more; a dashboard read on a stale shop enqueues one job, and returns at once with the store's data and its age. |

### 5.6 — Orders API

`GET /v1/shops/{shop_id}/orders` (filters: status, date range, tracking state; search by receipt number or SKU; cursor pagination) and `GET /v1/orders/{order_id}`. Buyer fields are masked unless the role allows them.

| ID | Acceptance criterion |
|---|---|
| AC-5.6.1 | Cross-tenant and non-granted shop requests return 404. |
| AC-5.6.2 | Viewer responses contain no buyer contact fields. |
| AC-5.6.3 | Every response includes `last_synced_at` for the shop. |

### 5.7 — Orders UI

- **Orders list:** filters, search, a freshness banner, empty states, and safe bulk actions: export selected to CSV with an audit record, and re-sync selected (through the engine).
- **Order detail:** items, shipments, a status timeline with sources, and a financial summary labelled estimated.

| ID | Acceptance criterion |
|---|---|
| AC-5.7.1 | Data older than the sync window shows a stale banner with the age. |
| AC-5.7.2 | Receipt numbers, SKUs and tracking numbers display LTR inside Hebrew UI. |
| AC-5.7.3 | Keyboard-only: find an order by search and open its detail. |
| AC-5.7.4 | Exporting selected orders produces a CSV with buyer fields masked by role and writes an audit record. |

### 5.8 — Drills

A restore drill (restore the latest backup into a scratch project and verify row counts and tenant isolation, including `etsy_order_buyers`) and a kill-worker drill in the deployed environment, with the results recorded.

| ID | Acceptance criterion |
|---|---|
| AC-5.8.1 | Both drills documented with date, duration, outcome and follow-ups in `docs/features/orders-ingest/operations.md`. |

### 5.9 — Queue, execution and sync health

Metrics are exposed in Prometheus format on the workers' health port and the API's internal metrics route (structlog stays the log format):

- queue depth and age, retries, dead-letter size;
- worker heartbeat age;
- executions per automation and action (success rate, latency p50 and p95);
- breaker state and rate-limit waits;
- webhook lag and sync freshness per shop.

Alerts go through the error-monitoring service's alert rules until Grafana arrives in Phase 8A. A minimal admin view lists dead-lettered jobs and executions.

| ID | Acceptance criterion |
|---|---|
| AC-5.9.1 | Stopping the worker beyond the heartbeat threshold sends one alert, and one recovery notice when it returns. |
| AC-5.9.2 | A dead-lettered job appears in the admin view with its last error and can be retried by an admin (audited). |
| AC-5.9.3 | A shop whose last successful sync is older than its window shows as stale in the UI and raises one alert. |
| AC-5.9.4 | `/metrics` exposes the series above with no organization or buyer data in labels (only IDs that are not personal, and shop IDs). |

### 5.10 — Durable automation engine (PR #58)

As implemented in #58, plus the 5.0 changes (`HandlerContext`, `WAITING`).

| ID | Acceptance criterion |
|---|---|
| AC-5.10.1 | Triggering the same `(automation_id, idempotency_key)` twice, including concurrently from two workers, creates one execution row and runs the handler once; the second call returns the first execution. |
| AC-5.10.2 | An automation whose readiness is not `ready`, whose handler is not deployed, or whose `callable_from` does not include the trigger kind is refused before any execution row is written, and the refusal is logged with the reason. |
| AC-5.10.3 | A user-action trigger from a role without the automation's capability returns 403 (the policy engine decides); a trigger naming a shop the user is not granted returns 404. |
| AC-5.10.4 | A handler that keeps failing follows the definition's retry policy (attempts, backoff bounds) and ends in `DEAD_LETTERED` with its last error; an admin retry is audited and reuses the same execution. |
| AC-5.10.5 | Killing the worker during a `RUNNING` execution leads, after the lease expires, to exactly one `SUCCEEDED` execution and one business effect. |
| AC-5.10.6 | Every execution state change is recorded with the actor (`ActorRef`) and the correlation ID; every transition outside the `ExecutionState` machine is rejected. |
| AC-5.10.7 | `htco registry sync --automations` shows a diff in dry-run, applies it idempotently and records the registry commit it came from; CI fails when the database seed differs from `registry/automations/`. |

### 5.11 — Order automations activated

The order work of slices 5.3 to 5.5 is packaged as the three automations above.

- The webhook and reconciliation paths trigger **Etsy Order Intake**, with idempotency key `etsy:{shop_id}:{receipt_id}:{event_type}`.
- Intake triggers **Order Deduplication & Verification** as a workflow step. It checks that exactly one logical order exists per receipt and that totals and items match the receipt, and records a discrepancy in `sync_drifts` otherwise.
- Paid, cancelled and refunded orders trigger **Product Sales Counters**.

Every business step of these automations goes through the Action Gateway (Internal and API channels). None has an AI step.

| ID | Acceptance criterion |
|---|---|
| AC-5.11.1 | The three specifications have no `draft` section; the registry loader accepts `readiness = "ready"` only with every section defined and the tests below passing. |
| AC-5.11.2 | **Scenario G** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)), against Postgres: one paid receipt replayed N times (N drawn by a property test, including concurrent and out-of-order deliveries through webhook and reconciliation) yields one order, correct item quantities, units sold and order count, one `orders.ingested.v1`, one `action_executions` row per business effect, and no duplicate outbox message or other side effect. |
| AC-5.11.3 | **Scenario B, part 1**: when the CRM write of Etsy Order Intake fails, the event stays in the inbox, the execution moves to `RETRY_SCHEDULED`, and the idempotent retry produces exactly one order; when retries are exhausted, the execution is dead-lettered and the event is still recoverable from the inbox by an audited admin retry. |
| AC-5.11.4 | Order Deduplication & Verification records one drift for a receipt whose stored totals or items differ from Etsy, and none for a matching receipt (table-driven tests). |
| AC-5.11.5 | Product Sales Counters never count a cancelled or refunded order, and replaying a cancellation does not decrement twice (property tests). |
| AC-5.11.6 | Each automation has a recovery runbook in `docs/features/orders-ingest/operations.md` and metrics visible in the 5.9 view. |
| AC-5.11.7 | An import-linter contract and a test prove that the three automations' handlers import nothing from `htco_agent_runtime`, the model gateway or Browser Use. |

### 5.12 — Action Gateway and the generic execution contract

- `htco_application.execution`: `ActionRequest`, `ActionResult`, `ActionDefinition`, `ChannelBinding` and `Evidence` as Pydantic v2 models, exported as JSON Schema to `packages/contracts/actions/`.
- The `ActionGateway.submit` algorithm of [execution-engine.md §4](../../architecture/execution-engine.md#4-gateway-algorithm).
- The table `app.action_executions`: unique `(action_name, idempotency_key)`, with state, channel, attempts, verification, evidence references, actor, correlation and causation IDs, the approval ID, and outcome. It has a transition trigger and RLS by organization and shop.
- Action definitions are built from the registry's tool definitions plus a bindings file per family (`registry/actions/*.toml`, reviewed data).

| ID | Acceptance criterion |
|---|---|
| AC-5.12.1 | Submitting the same `(action, idempotency_key)` twice, including concurrently, executes once and returns the first result. |
| AC-5.12.2 | An unknown action or version, input that fails its schema, a user role without the capability, a non-granted shop (404), or an advisor actor is `DENIED` before any channel runs, with the reason recorded. |
| AC-5.12.3 | A W-policy action without `approval_id` or `owner_trigger` returns `PENDING_APPROVAL` and runs nothing; resubmitted with a decided approval, every check runs again at execution time. |
| AC-5.12.4 | Every result writes one `action_executions` row, one audit record for W-policies and personal data, and emits `actions.executed.v1` through the outbox; secrets never appear in any of them (log-capture test). |
| AC-5.12.5 | **Scenario H (gateway)**: an automation actor requesting an action outside its specification's permissions, and an employee fixture requesting a tool outside its grants, are both denied by the gateway. |

### 5.13 — Execution Router and channel adapters

- **The router:** chooses a binding deterministically (bindings in order, shop capabilities, environment status, browser scope flag, and actor kind: automations get deterministic bindings only).
- **The API adapter:** real. Etsy reads go through the guarded adapter; writes go through the outbox, with one `Deliverer` per write action. All writes stay disabled until Phase 6 and 7's owner-triggered slices.
- **The Internal adapter:** use cases.
- **The Browser adapter:** the `BrowserChannel` Protocol and a `FakeBrowserChannel` that replays recorded recipe outcomes, including a security-challenge outcome.
- **The Human adapter:** the contract that creates a task (Phase 7 store; in-memory now) and resumes on completion.

| ID | Acceptance criterion |
|---|---|
| AC-5.13.1 | A contract test runs one fixture action through the API, Internal and fake Browser adapters and gets the same `ActionResult` shape, audit and activity output. |
| AC-5.13.2 | An automation can never be routed to a non-deterministic binding (Browser Use); the router refuses with `scope_missing` or the next deterministic binding. |
| AC-5.13.3 | A browser binding is never chosen when the environment is not `READY`, the action's flag is off, or its Etsy scope row is missing or older than the flag. |
| AC-5.13.4 | The fake Browser adapter's challenge outcome stops the action, returns `HANDED_TO_HUMAN`, creates one Human task and retries nothing. |
| AC-5.13.5 | An API refusal for a missing scope or capability never falls through to a browser binding unless that binding has its own recorded scope. |

### 5.14 — Shared approval contract

`ApprovalService` and `ApprovalRequest` as in [execution-engine.md §5](../../architecture/execution-engine.md#5-approval-contract), with an in-memory implementation used by the gateway's tests. The Postgres store, the inbox UI and step-up rules follow in Phase 7. The contract is the same for users, automations, employees and browser actions.

| ID | Acceptance criterion |
|---|---|
| AC-5.14.1 | The request hash covers every parameter that will be sent; a resubmission whose request differs from the approved hash is refused. |
| AC-5.14.2 | An approved request executes at most once (the second resubmission returns the first result). |
| AC-5.14.3 | An automation execution waiting for an approval is `WAITING` and holds no job lease; on decision it resumes once; on rejection or expiry it is `CANCELLED` with the reason. |

### 5.15 — Circuit breaker, rate and concurrency control, idempotency

- **PyBreaker**, with state shared in Postgres, per key: `etsy:api`, `etsy:web`, and `browser:{shop}` later.
- **aiolimiter** in each worker, plus a Postgres token bucket for the app-wide Etsy budget (QPS and QPD across every shop and worker; the real figures come from the Developer Portal).
- **Concurrency slots:** per-shop writes, and per-profile browser sessions (advisory lock, used from 8A).
- **Tenacity** for bounded retries inside an attempt.
- Valkey is not used; the limiter port allows it later.

| ID | Acceptance criterion |
|---|---|
| AC-5.15.1 | With two workers and four shops, total Etsy calls never exceed the configured QPS in any second, or the QPD in a day (simulated clock). |
| AC-5.15.2 | Five failures in 60 s open the breaker for that key; calls return `DEFERRED` with `retry_at`; half-open after the cooldown lets one call through. |
| AC-5.15.3 | A 4xx other than 429 is never retried; a 429 waits for `Retry-After`. |
| AC-5.15.4 | Two writes to the same shop never run at the same time when the action has a per-shop write slot. |

### 5.16 — Shop Environment Registry (contracts only)

- Tables `app.shop_browser_environments` and `app.browser_environment_events`, with the seven statuses and the transition table of [execution-engine.md §7](../../architecture/execution-engine.md#7-state-machines), enforced by a database trigger and RLS (Owner and Admin of the organization).
- The `ShopEnvironmentRegistry` and `EnvironmentProvisioner` Protocols, with an in-memory provisioner fake.
- A read API, `GET /v1/shops/{shop_id}/browser-environment`, returns the status only.
- No browser runs, and no creation endpoint is enabled; creation is behind the `browser_environments` flag, which stays off until Phase 8A.

| ID | Acceptance criterion |
|---|---|
| AC-5.16.1 | Every allowed transition succeeds, and every other one is refused by the domain and by the database. |
| AC-5.16.2 | Every transition records actor, reason and correlation ID; the history is append-only. |
| AC-5.16.3 | A shop of organization A cannot read or change B's environment (pgTAP). |
| AC-5.16.4 | Adding a shop still needs OAuth only; a shop without an environment is `NOT_CONFIGURED`, and every API-channel action works unchanged. |

### 5.17 — Secret Broker and Authenticator contracts

The `SecretBroker` and `Authenticator` Protocols, `InjectionGrant`, `SecretRef`, `DisplayedCode`, `ConsentRecord` and `SecretValue` (no `__repr__`, no serialization), with fakes. Tables are defined in DATA_MODEL and created in Phase 8B. A static check proves that no tool definition or action output schema has a field typed as a secret or a code.

| ID | Acceptance criterion |
|---|---|
| AC-5.17.1 | `SecretValue` never appears in `repr`, logs, JSON or exception messages (property tests over the fakes and log capture). |
| AC-5.17.2 | A grant is single use, bound to one execution and environment, and expires within 60 s (fake broker tests). |
| AC-5.17.3 | The registry check fails if any tool's output contract can carry a secret or a one-time code. |

### 5.18 — Status management for tasks, executions and environments

- One status vocabulary across the CRM:
  - execution states (`ExecutionState`, now with `WAITING`);
  - action execution states;
  - environment statuses;
  - the Human-task waiting reasons.
- Status changes are published as events (`automations.execution_state_changed.v1`, `actions.executed.v1`, `browser.environment_status_changed.v1`).
- One read API for the office: `GET /v1/executions/{id}` returns the full chain (trigger, execution, actions, approvals, human tasks).

| ID | Acceptance criterion |
|---|---|
| AC-5.18.1 | For one order, the chain view shows the webhook, the intake execution, every action with its channel and outcome, and the resulting events, in causal order. |
| AC-5.18.2 | Every status change of an execution, an action or an environment emits exactly one event (coverage test). |

## Test plan

- Domain property tests for order state and counts.
- Engine and gateway tests against Postgres (replay, concurrency, retry, dead letter, lease expiry, waiting and resume).
- Router contract tests across the adapters.
- Limiter and breaker tests with a simulated clock.
- Integration tests for the queue, inbox, outbox and ingest against the local stack.
- Webhook security tests, with recorded payloads for all 11 events.
- Crash and replay tests.
- Contract tests for the Etsy adapter.
- API and UI tests.
- Mutation testing starts nightly on the order, money and gateway modules.
- Manual verification with real orders in production (read only).

## Security checklist

- [ ] Webhook verification before any processing (ASVS V4, V2.3); request body size limit.
- [ ] Workers use their own database role and explicit tenant filters; tested.
- [ ] Buyer PII masked by role; never logged; no contact data stored (ADR-0018).
- [ ] The gateway is the only path to Etsy for automations; an architecture test forbids importing the Etsy adapter outside the API channel and the maintenance probes.
- [ ] No secret or code type reachable by a tool or model (5.17).

## Documentation

- `docs/features/job-queue/`, `docs/features/automation-engine/`, `docs/features/orders-ingest/` and `docs/features/execution-gateway/`.
- The three automation specifications completed in `registry/automations/`.
- ADR-0013 and ADR-0019 validation logs.
- EVENT_CATALOG entries, DATA_MODEL tables marked created, and `packages/contracts/actions/`.

## Demo

1. Replay a webhook ten times and show one order, one execution per automation and one action per effect (Scenario G).
2. Inject a failed CRM write and show the event recovered from the inbox.
3. Disable webhooks and show reconciliation catching up.
4. Kill the worker mid-job and show it recover.
5. Show the fake Browser channel's challenge handed to a person.
6. Show the chain view of one order.
7. Browse real orders in Hebrew and English.

## Exit gate

- [ ] All AC-5.x pass, including Scenario G and Scenario B part 1 against Postgres; drills recorded; KPIs baselined (orders per day, time to tracking).
- [ ] PRs #53, #55 and #58 merged only after 5.0.
- [ ] The three order automations are `ready` with fully defined specifications, and the owner approved their activation.

## Rollback

- Disable webhook processing by flag.
- Set an automation's readiness back to `draft` through the registry sync: new triggers are refused, and running executions finish.
- Reconciliation can rebuild orders from Etsy, because ingest is idempotent.
- The gateway has no state to roll back beyond `action_executions`. Disabling an action's binding falls back to the next binding or `DENIED`.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy webhook payload or delivery problems (reported by other developers) | Reconciliation is always on |
| The gateway adds latency to every action | Internal actions skip the network guards. A p95 budget of 5 ms gateway overhead is measured in 5.12 |
| The contract slices grow into a browser build | 5.13, 5.16 and 5.17 ship Protocols and fakes only. Any Playwright import in Phase 5 fails the architecture test |
| Aligning the three PRs delays the phase | The changes are additive and listed precisely. 5.0 is time-boxed to three days |

## Cost impact

- `agent-worker` component about $5–12/month (PR #54).
- Automations make no model calls.
- Prometheus metrics are exposed only; no paid monitoring.
- The gateway, limiter and breakers are OSS libraries (aiolimiter MIT, PyBreaker BSD, Tenacity Apache-2.0).
