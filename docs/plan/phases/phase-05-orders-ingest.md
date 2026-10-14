# Phase 5 — Automation engine and orders CRM

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | Phase 4 |
| **Requirements** | FR-AUT-01, FR-AUT-02, FR-AUT-03, FR-AUT-04, FR-AUT-05, FR-FIN-01, FR-ORD-01, FR-ORD-02, FR-ORD-03, FR-ORD-04, FR-ORD-05, FR-ORD-08, FR-ORD-09, FR-SHOP-04, FR-TASK-03 |
| **Status** | Not started |

## Progress

- [ ] 5.1 Job queue and worker
- [ ] 5.2 Inbox and outbox
- [ ] 5.3 Webhook ingress
- [ ] 5.4 Receipt ingest
- [ ] 5.5 Reconciliation
- [ ] 5.6 Orders API
- [ ] 5.7 Orders UI
- [ ] 5.8 Drills
- [ ] 5.9 Queue and sync health
- [ ] 5.10 Durable automation engine
- [ ] 5.11 Order automations activated

Slice 5.10 is built right after 5.2, because slices 5.3 to 5.5 run as automations on it. Slice 5.11 completes the specifications, tests and runbooks of the three order automations and activates them.

**Foundation already in code.** The automation model and an in-memory engine exist: `htco_domain.automation` (`AutomationDefinition`, `AutomationExecution`, `ExecutionState`, `TriggerKind`) and `htco_application.automation.engine` (`AutomationEngine`, the `ExecutionStore` port), tested in `tests/application/test_automation_engine.py`; the draft specifications are in [`registry/automations/`](../../../registry/automations) and validated by `tests/tools/test_registry_files.py`. This phase adds the Postgres store, the job queue underneath and the first three production automations.

## Goal

Every paid Etsy order appears in HTCO exactly once, with its items, its current state, its source and how fresh it is, whether Etsy's webhook arrived, arrived twice, arrived late or never arrived. The background machinery (job queue, worker, inbox, outbox) is built here and proven with crash and replay tests. On top of it runs the durable automation engine of [ADR-0013](../../adr/0013-automation-engine.md): order work is done by three deterministic automations (Etsy Order Intake, Order Deduplication & Verification, Product Sales Counters), with no model calls.

## Scope

**In:** job queue spike and implementation (ADR-0007), durable automation engine (`automation_definitions` and `automation_executions` tables, `ExecutionStore` on Postgres, triggers from events, schedules and user actions through the API with authorization), worker process and deployment, inbox and outbox, webhook ingress (if available), receipt ingest, reconciliation poller and nightly sweep, order state machine and status events, basic financial entries, orders API and UI, restore and kill-worker drills.

**Out:** tracking detection and incidents (Phase 6), approvals and the activity record store (Phase 7), employee triggers through the `automation.trigger` tool (Phase 8; the engine already checks them), the full registry sync for employees and tools (Phase 8).

## Automations activated

| Automation | Registry | Slices | Business owner |
|---|---|---|---|
| Etsy Order Intake | [`etsy-order-intake.toml`](../../../registry/automations/etsy-order-intake.toml) | 5.3, 5.4, 5.5 | Eitan (the owner is accountable until Eitan is active in Phase 9) |
| Order Deduplication & Verification | [`order-dedup-verification.toml`](../../../registry/automations/order-dedup-verification.toml) | 5.4, 5.5 | Eitan (as above) |
| Product Sales Counters | [`product-sales-counters.toml`](../../../registry/automations/product-sales-counters.toml) | 5.4 | Roni (the owner is accountable until Roni is active in Phase 13) |

An automation is activated only when all 18 sections of its specification are `defined` (no `draft` left), its input and output schemas are in `packages/contracts/automations/`, its replay, failure and recovery tests pass, its runbook exists, and the owner approves the activation (ADR-0012 rule 2; [PLAN §5](../PLAN.md#5-gates-that-apply-to-every-phase) gate 7).

## Owner prerequisites

- Webhook endpoint registered in the Etsy developer portal, if available for the Seller App.

## Slices

### 5.1 — Job queue and worker

Time-boxed spike comparing the options in ADR-0007; `JobQueue` port and adapter; worker process with claim and lease, heartbeat, retries with backoff and jitter, dead-letter state, per-resource locks, graceful shutdown, scheduled jobs; the `agent-worker` component (named `worker` in version 1.0 of the plan; ADR-0012 rule 7) deployed with a health port; `worker_heartbeats`; scheduled maintenance jobs (daily capability probes, refresh-token expiry checks, purge of disconnected shops' data after their purge date).

| ID | Acceptance criterion |
|---|---|
| AC-5.1.1 | Killing the worker mid-job leads to exactly one successful completion after restart (automated kill test). |
| AC-5.1.2 | Two workers never run the same job or the same per-shop lock concurrently. |
| AC-5.1.3 | A job that fails N times lands in the dead-letter state with its last error, visible to admins. |
| AC-5.1.4 | SIGTERM lets the current job finish (within a deadline) before exit. |
| AC-5.1.5 | ADR-0007 updated to Accepted with the spike results. |
| AC-5.1.6 | Scheduled maintenance jobs run once per period even with two worker instances; the purge deletes only the disconnected shop's data and only after its purge date. |

### 5.2 — Inbox and outbox

`inbox` unique by `(source, event_id)`; `outbox` unique by idempotency key with delivery status, attempts and next attempt time; delivery job. `Idempotency-Key` handling for unsafe API requests: the request hash and response are stored for 24 hours and replayed on retry.

| ID | Acceptance criterion |
|---|---|
| AC-5.2.1 | Inserting the same inbox event twice results in one row and one job. |
| AC-5.2.2 | A crash after the business commit but before delivery results in exactly one delivery after restart. |
| AC-5.2.3 | Repeating a POST with the same `Idempotency-Key` returns the stored response with no second effect; the same key with a different body is rejected. |

### 5.3 — Webhook ingress

`POST /webhooks/etsy`: verify Standard Webhooks signature on the raw body (5-minute tolerance), store in inbox, enqueue ingest keyed by `(shop_id, receipt_id)`, respond 2xx within a second. If Etsy webhooks are unavailable for our app, this slice ships disabled behind a flag.

| ID | Acceptance criterion |
|---|---|
| AC-5.3.1 | Invalid signature, modified body, timestamp outside tolerance and replayed `webhook-id` are rejected or ignored as documented, and logged without the body. |
| AC-5.3.2 | p95 response time under 1 s with 20 concurrent valid webhooks. |
| AC-5.3.3 | Requests above the per-source rate limit get 429 and are logged; Etsy's retries are accepted after the window. |

### 5.4 — Receipt ingest

Fetch the receipt; upsert `orders`, `order_items`, `shipments` in one transaction; apply only if Etsy's `updated_timestamp` is newer; record `order_status_events`; store raw payload hash; create basic `financial_entries` (revenue, Etsy fees) labeled `ESTIMATED`; emit `orders.ingested.v1` or `orders.updated.v1`.

| ID | Acceptance criterion |
|---|---|
| AC-5.4.1 | The same receipt event processed 10 times yields one order, correct item quantities and one `orders.ingested.v1`. |
| AC-5.4.2 | An older update arriving after a newer one does not overwrite newer data. |
| AC-5.4.3 | Units sold and order counts are computed separately and exclude cancelled and refunded orders (property tests). |
| AC-5.4.4 | All money uses `Decimal` with currency; a float anywhere in the money path fails a test. |
| AC-5.4.5 | Every allowed order transition (FR-ORD-03) is tested, each illegal transition is rejected, and every transition records its source and `observed_at`. |

### 5.5 — Reconciliation

Scheduled job per shop: receipts modified since `cursor − margin`, sorted by update time, paged; nightly 30-day sweep; `sync_cursors`; freshness metric per shop.

| ID | Acceptance criterion |
|---|---|
| AC-5.5.1 | With webhooks disabled, every receipt in a 200-receipt fixture is ingested by reconciliation alone. |
| AC-5.5.2 | A receipt whose webhook was lost is ingested within one reconciliation interval. |
| AC-5.5.3 | The daily Etsy call count for the pilot shop stays inside the rate-limit budget (computed in a test from the schedule). |
| AC-5.5.4 | A difference between a stored order and Etsy (status, items or shipments) is recorded in `sync_drifts` with both values and emits `orders.drift_detected.v1`; from Phase 6 it opens an incident (FR-ORD-09). |

### 5.6 — Orders API

`GET /v1/shops/{shop_id}/orders` (filters: status, date range, tracking state; search by receipt number or SKU; cursor pagination); `GET /v1/orders/{order_id}`; buyer fields masked unless the role allows.

| ID | Acceptance criterion |
|---|---|
| AC-5.6.1 | Cross-tenant and non-granted shop requests return 404. |
| AC-5.6.2 | Viewer responses contain no buyer contact fields. |
| AC-5.6.3 | Every response includes `last_synced_at` for the shop. |

### 5.7 — Orders UI

Orders list (filters, search, freshness banner, empty states, safe bulk actions: export selected to CSV with an audit record, and re-sync selected) and order detail (items, shipments, status timeline with sources, financial summary labeled estimated).

| ID | Acceptance criterion |
|---|---|
| AC-5.7.1 | Data older than the sync window shows a stale banner with the age. |
| AC-5.7.2 | Receipt numbers, SKUs and tracking numbers display LTR inside Hebrew UI. |
| AC-5.7.3 | Keyboard-only: find an order by search and open its detail. |
| AC-5.7.4 | Exporting selected orders produces a CSV with buyer fields masked by role and writes an audit record. |

### 5.8 — Drills

Restore drill (restore the latest backup into a scratch project and verify row counts and tenant isolation) and kill-worker drill in the deployed environment; results recorded.

| ID | Acceptance criterion |
|---|---|
| AC-5.8.1 | Both drills documented with date, duration, outcome and follow-ups in `docs/features/orders-ingest/operations.md`. |

### 5.9 — Queue and sync health

Metrics for queue depth and age, retries, dead-letter size, worker heartbeat age, webhook lag and sync freshness per shop; alerts when thresholds are crossed (through the error-monitoring service's alert rules until email notifications arrive in Phase 6); a minimal admin view of dead-lettered jobs.

| ID | Acceptance criterion |
|---|---|
| AC-5.9.1 | Stopping the worker beyond the heartbeat threshold sends one alert, and one recovery notice when it returns. |
| AC-5.9.2 | A dead-lettered job appears in the admin view with its last error and can be retried by an admin (audited). |
| AC-5.9.3 | A shop whose last successful sync is older than its window shows as stale in the UI and raises one alert. |

### 5.10 — Durable automation engine

Migrations for `automation_definitions` (ID, version, business owner, primary trigger, `callable_from`, retry policy, timeout, side-effect class, approval requirement, readiness, activation phase, specification hash) and `automation_executions` (state machine of `ExecutionState`, attempts, actor kind and ID, trigger kind, correlation and causation IDs, last error, timestamps), unique by `(automation_id, idempotency_key)`. `automation_definitions` rows are loaded from `registry/automations/` by a reviewed sync command (`htco registry sync --automations`, dry-run first, applied by the migration workflow); rows are never edited by hand. `ExecutionStore` adapter on Postgres; each execution is a job on the queue of slice 5.1 (lease, heartbeat, backoff with jitter, dead letter); handlers call use cases and send external effects through the outbox of slice 5.2. Triggers: inbox events, schedules and user actions, all entering through `AutomationEngine.trigger`; user actions come through an API route that authorizes the caller (`POST /v1/automations/{automation_id}/executions`, with `Idempotency-Key`). Read API: `GET /v1/automations` and `GET /v1/automations/{automation_id}/executions`.

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

The order work of slices 5.3 to 5.5 is packaged as the three automations above. The webhook and reconciliation paths trigger Etsy Order Intake (idempotency key `etsy:{shop_id}:{receipt_id}:{event_type}`). Intake triggers Order Deduplication & Verification as a workflow step: it checks that exactly one logical order exists per receipt and that totals and items match the receipt, and records a discrepancy in `sync_drifts` otherwise (an incident from Phase 6, a quality finding for Noa from Phase 10). Paid, cancelled and refunded orders trigger Product Sales Counters, which update unit and order counts per listing and SKU from `order_items.quantity`, net of cancellations and refunds (per product once the catalog model exists in Phase 12). Each specification is completed to all 18 sections, with schemas, tests and a recovery runbook.

| ID | Acceptance criterion |
|---|---|
| AC-5.11.1 | The three specifications have no `draft` section; the registry loader accepts `readiness = "ready"` only with every section defined and the tests below passing (the domain refuses otherwise). |
| AC-5.11.2 | **Scenario G** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)), against Postgres: one paid receipt replayed N times (N drawn by a property test, including concurrent and out-of-order deliveries through webhook and reconciliation) yields one order, correct item quantities, units sold and order count, one `orders.ingested.v1`, and no duplicate outbox message or other side effect. The in-memory version in `tests/application/test_automation_engine.py` keeps passing. |
| AC-5.11.3 | **Scenario B, part 1**: when the CRM write of Etsy Order Intake fails (database error injected after the inbox insert), the event stays in the inbox, the execution moves to `RETRY_SCHEDULED`, and the idempotent retry produces exactly one order; when retries are exhausted, the execution is dead-lettered and the event is still recoverable from the inbox by an audited admin retry. The final part of Scenario B (the nightly supervisor finds and verifies the missing record) is in Phase 10. |
| AC-5.11.4 | Order Deduplication & Verification records one drift for a receipt whose stored totals or items differ from Etsy, and none for a matching receipt (table-driven tests). |
| AC-5.11.5 | Product Sales Counters never count a cancelled or refunded order, and replaying a cancellation does not decrement twice (property tests). |
| AC-5.11.6 | Each automation has a recovery runbook in `docs/features/orders-ingest/operations.md` and metrics (executions, success rate, retries, dead letters, latency p50 and p95) visible in the queue health view of slice 5.9. |

## Test plan

Domain property tests for order state and counts; engine tests against Postgres (replay, concurrency, retry, dead letter, lease expiry) reusing the in-memory engine's test cases; integration tests for queue, inbox, outbox and ingest against the local stack; webhook security tests; crash and replay tests; contract tests for the Etsy adapter; API and UI tests; mutation testing starts nightly on the order and money modules; manual verification with real orders in production.

## Security checklist

- [ ] Webhook verification before any processing (ASVS V4, V2.3); request body size limit.
- [ ] Workers use their own database role and explicit tenant filters; tested.
- [ ] Buyer PII masked by role; never logged.

## Documentation

`docs/features/job-queue/`, `docs/features/automation-engine/`, `docs/features/orders-ingest/`; the three automation specifications completed in `registry/automations/`; ADR-0013 validation log updated with the Postgres results; EVENT_CATALOG entries; DATA_MODEL tables marked created; ADR-0007 accepted.

## Demo

Replay a webhook ten times and show one order and one execution per automation (Scenario G); inject a failed CRM write and show the event recovered from the inbox (Scenario B, part 1); disable webhooks and show reconciliation catching up; kill the worker mid-job and show it recover; browse real orders in Hebrew and English.

## Exit gate

- [ ] All AC-5.x pass, including Scenario G and Scenario B part 1 against Postgres; drills recorded; KPIs baselined (orders per day, time to tracking).
- [ ] The three order automations are `ready` with fully defined specifications, and the owner approved their activation.

## Rollback

Disable webhook processing by flag; set an automation's readiness back to `draft` through the registry sync (new triggers are refused, running executions finish); reconciliation can rebuild orders from Etsy because ingest is idempotent.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy webhook payload or delivery problems (reported by other developers) | Reconciliation is always on |
| Queue implementation choice delays the phase | Spike is time-boxed to two days |
| Owning the engine's durability costs more than expected | The engine stays small (one entry point, one state machine); ADR-0013 can be superseded by an external engine if the spike or drills show gaps |

## Cost impact

`agent-worker` component about $5–12/month. Automations make no model calls.
