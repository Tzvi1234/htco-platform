# Phase 5 — Orders CRM

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | Phase 4 |
| **Requirements** | FR-FIN-01, FR-ORD-01, FR-ORD-02, FR-ORD-03, FR-ORD-04, FR-ORD-05, FR-ORD-08, FR-ORD-09, FR-SHOP-04, FR-TASK-03 |
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

## Goal

Every paid Etsy order appears in HTCO exactly once, with its items, its current state, its source and how fresh it is, whether Etsy's webhook arrived, arrived twice, arrived late or never arrived. The background machinery (job queue, worker, inbox, outbox) is built here and proven with crash and replay tests.

## Scope

**In:** job queue spike and implementation (ADR-0007), worker process and deployment, inbox and outbox, webhook ingress (if available), receipt ingest, reconciliation poller and nightly sweep, order state machine and status events, basic financial entries, orders API and UI, restore and kill-worker drills.

**Out:** tracking detection and incidents (Phase 6), approvals (Phase 7).

## Owner prerequisites

- Webhook endpoint registered in the Etsy developer portal, if available for the Seller App.

## Slices

### 5.1 — Job queue and worker

Time-boxed spike comparing the options in ADR-0007; `JobQueue` port and adapter; worker process with claim and lease, heartbeat, retries with backoff and jitter, dead-letter state, per-resource locks, graceful shutdown, scheduled jobs; `worker` component deployed with a health port; `worker_heartbeats`; scheduled maintenance jobs (daily capability probes, refresh-token expiry checks, purge of disconnected shops' data after their purge date).

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

## Test plan

Domain property tests for order state and counts; integration tests for queue, inbox, outbox and ingest against the local stack; webhook security tests; crash and replay tests; contract tests for the Etsy adapter; API and UI tests; mutation testing starts nightly on the order and money modules; manual verification with real orders in production.

## Security checklist

- [ ] Webhook verification before any processing (ASVS V4, V2.3); request body size limit.
- [ ] Workers use their own database role and explicit tenant filters; tested.
- [ ] Buyer PII masked by role; never logged.

## Documentation

`docs/features/job-queue/`, `docs/features/orders-ingest/`; EVENT_CATALOG entries; DATA_MODEL tables marked created; ADR-0007 accepted.

## Demo

Replay a webhook ten times and show one order; disable webhooks and show reconciliation catching up; kill the worker mid-job and show it recover; browse real orders in Hebrew and English.

## Exit gate

- [ ] All AC-5.x pass; drills recorded; KPIs baselined (orders per day, time to tracking).

## Rollback

Disable webhook processing by flag; reconciliation can rebuild orders from Etsy because ingest is idempotent.

## Risks

| Risk | Mitigation |
|---|---|
| Etsy webhook payload or delivery problems (reported by other developers) | Reconciliation is always on |
| Queue implementation choice delays the phase | Spike is time-boxed to two days |

## Cost impact

`worker` component about $5–12/month.
