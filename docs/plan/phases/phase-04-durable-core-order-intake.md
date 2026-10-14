# Phase 4 — Durable core and order intake

| | |
|---|---|
| **Milestone** | — (M1 Foundation closes at the end of [Phase 5](phase-05-tasks-messages-approvals.md)) |
| **Repositories** | `htco-platform` (most work), `htco-clients` (orders and queue health screens) |
| **Size** | L |
| **Depends on** | [Phase 2](phase-02-identity-tenancy.md) (database, RLS, audit); [Phase 3](phase-03-design-system-web-shell.md) for the screens of slices 4.10 and 4.11 |
| **Requirements** | FR-AUT-01, FR-AUT-02, FR-AUT-03, FR-AUT-04, FR-AUT-05, FR-FIN-01, FR-ORD-01, FR-ORD-02, FR-ORD-03, FR-ORD-04, FR-ORD-05, FR-ORD-08, FR-ORD-09, FR-TASK-03 |
| **Mandatory tests owned** ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) | **Test 1** with signed recorded fixtures (the real-webhook run is [Phase 9](phase-09-etsy-connection.md)); **Test 13**, the first restore drill, before any real data |
| **Status** | Not started |

## Progress

- [ ] 4.1 Job queue spike, port and worker (ADR-0007)
- [ ] 4.2 Inbox and outbox
- [ ] 4.3 Durable automation engine
- [ ] 4.4 Recorded Etsy fixtures and the receipt source port
- [ ] 4.5 Webhook ingress (signed fixtures)
- [ ] 4.6 Receipt ingest
- [ ] 4.7 Reconciliation
- [ ] 4.8 Order automations activated
- [ ] 4.9 Orders API
- [ ] 4.10 Orders UI
- [ ] 4.11 Queue and sync health
- [ ] 4.12 Restore drill and kill-worker drill

Slices 4.1 to 4.3 are the durable core and come first, in that order: everything after them runs as jobs and automations on top. Slice 4.4 can be built in parallel with 4.1–4.3 because it is pure fixtures and a port. Slice 4.12 must pass before [Phase 9](phase-09-etsy-connection.md) connects the real shop.

**Foundation already in code.** The automation model and an in-memory engine exist: `htco_domain.automation` (`AutomationDefinition`, `AutomationExecution`, `ExecutionState`, `TriggerKind`) and `htco_application.automation.engine` (`AutomationEngine`, the `ExecutionStore` port), tested in `tests/application/test_automation_engine.py`. The draft specifications are in [`registry/automations/`](../../../registry/automations) and validated by `tests/tools/test_registry_files.py`. This phase adds the Postgres store, the job queue underneath, the order tables and the first three production automations.

## Goal

The durable core of the office exists and is proven by crash and replay tests: a job queue with leases and heartbeats, an inbox and an outbox, and the durable automation engine of [ADR-0013](../../adr/0013-automation-engine.md). On top of it, every paid Etsy order that reaches HTCO becomes exactly one order, with its items, its current state, its source and how fresh it is, whether the webhook arrived once, three times, late or never.

Etsy itself is **not connected** in this phase. Orders arrive as **signed, recorded fixtures**: real receipt and webhook payloads recorded read-only from the owner's shop in the Phase 0 spike, scrubbed, and replayed through the same ingress, signature check, inbox and automations that real traffic will use. [Phase 9](phase-09-etsy-connection.md) connects the real shop and switches the same intake to real webhooks and reconciliation, without changing the intake code above the port.

Order work is done by three deterministic automations (Etsy Order Intake, Order Deduplication & Verification, Product Sales Counters), with no model calls.

## Why now

Every later phase needs durable background work: the task dispatcher of [Phase 5](phase-05-tasks-messages-approvals.md) leases agent steps on this queue, the Claude runtime of [Phase 6](phase-06-workforce-claude-runtime.md) runs its steps as jobs, and nightly supervision in [Phase 7](phase-07-pilot-office.md) reads queue health. Order intake is the simplest real workload that proves the core: it is deterministic, high-stakes (a duplicated or lost order is a business error) and easy to replay. Building it on recorded fixtures first keeps the core work off the critical path of the Etsy Seller App approval, and lets the first restore drill run before any real data exists, as the master plan requires.

## Scope

**In:** job queue spike and implementation ([ADR-0007](../../adr/0007-job-queue.md)); `JobQueue` port and adapter; worker process (`workers/agents`, deployed as the `agent-worker` component of `htco-dev`); inbox and outbox; `Idempotency-Key` handling for unsafe API requests; durable automation engine (`automation_definitions`, `automation_executions`, `ExecutionStore` on Postgres, triggers from events, schedules and user actions); recorded and scrubbed Etsy fixtures with a provenance manifest; a `MarketplaceReceiptsPort` with a fixture adapter; webhook ingress with signature verification; receipt ingest; order state machine and status events; basic `ESTIMATED` financial entries; reconciliation logic and `sync_cursors` and `sync_drifts` (run against the fixture adapter); the three order automations; orders API and UI; queue and sync health; restore drill (Test 13) and kill-worker drill.

**Out:** the real Etsy connection, OAuth, token vault, real webhook registration, capability probes and Etsy maintenance jobs (refresh-token expiry, disconnect purge) ([Phase 9](phase-09-etsy-connection.md)); tracking detection and incidents ([Phase 10](phase-10-fulfillment-incidents.md)); business tasks, the task dispatcher, approvals and activity records ([Phase 5](phase-05-tasks-messages-approvals.md)); employee triggers through the `automation.trigger` tool (the engine already checks them; the tool arrives in [Phase 6](phase-06-workforce-claude-runtime.md)); any model call; any write to Etsy.

## Automations activated

| Automation | Registry | Slices | Business owner |
|---|---|---|---|
| Etsy Order Intake | [`etsy-order-intake.toml`](../../../registry/automations/etsy-order-intake.toml) | 4.5, 4.6, 4.7, 4.8 | Eitan (the owner is accountable until Eitan is active in [Phase 10](phase-10-fulfillment-incidents.md)) |
| Order Deduplication & Verification | [`order-dedup-verification.toml`](../../../registry/automations/order-dedup-verification.toml) | 4.6, 4.7, 4.8 | Eitan (as above) |
| Product Sales Counters | [`product-sales-counters.toml`](../../../registry/automations/product-sales-counters.toml) | 4.6, 4.8 | Roni (the owner is accountable until Roni is active in [Phase 13](phase-13-procurement-finance.md)) |

An automation is activated only when all 18 sections of its specification are `defined` (no `draft` left), its input and output schemas are in `packages/contracts/automations/`, its replay, failure and recovery tests pass, its runbook exists, and the owner approves the activation ([ADR-0012](../../adr/0012-living-office-organization.md) rule 2; [PLAN §6](../PLAN.md#6-gates-that-apply-to-every-phase) gate 7). In this phase "activated" means running in `htco-dev` on fixture traffic; the same automations take real traffic in Phase 9 without a new activation, unless their specification changes.

## Owner prerequisites

| # | Task | Why |
|---|---|---|
| P4-1 | Supabase Pro **development** project exists (Phase 0 O-13) and Phase 2's migrations are applied | The queue, inbox and order tables live there |
| P4-2 | Decide the target **RTO and RPO** for the database (proposal written by the agent at kickoff from the plan's backup facts) | Test 13 measures the drill against them; no production project before they exist ([Phase 0](phase-00-groundwork.md) O-13) |
| P4-3 | Approve adding the `agent-worker` component to the `htco-dev` app (Phase 0 O-14); the agent states the size and monthly price, verified at kickoff | New monthly cost |
| P4-4 | The Phase 0 Etsy spike (slice 0.3) recorded receipts read-only, or the owner approves synthetic fixtures built from Etsy's documented schemas until it does. **Approved 2026-10-14:** synthetic fixtures from the documented schemas; Phase 9 re-runs the same contract tests on real traffic | Slice 4.4 needs fixtures |
| P4-5 | Approve the activation of the three order automations (end of slice 4.8) | Gate 7 |

The webhook endpoint registration in the Etsy developer portal moved to [Phase 9](phase-09-etsy-connection.md).

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`. For slices 4.10 and 4.11 in `htco-clients`, also load `design-system`.
2. Read, in this order: this file; [PLAN.md](../PLAN.md) §1, §5 and §6; [ADR-0007](../../adr/0007-job-queue.md), [ADR-0008](../../adr/0008-etsy-integration.md), [ADR-0011](../../adr/0011-data-access-and-migrations.md), [ADR-0012](../../adr/0012-living-office-organization.md), [ADR-0013](../../adr/0013-automation-engine.md); [DATA_MODEL](../../architecture/DATA_MODEL.md) (orders, infrastructure tables), [EVENT_CATALOG](../../architecture/EVENT_CATALOG.md) (`orders.*`), the FR IDs above in the [PRD](../../product/PRD.md), [TESTING_STRATEGY](../../engineering/TESTING_STRATEGY.md) §10 (Scenarios B and G) and the three automation specifications in `registry/automations/`.
3. At kickoff, refresh facts and write them down: the current status and features of Supabase Queues, `procrastinate` and any other option in ADR-0007; Etsy's current webhook signature scheme and event names (the version 2.0 plan assumed Standard Webhooks; verify, do not assume); the DigitalOcean worker size and price. Record each in the ADR or feature doc with the date.
4. Build the slices in order: 4.1, 4.2, 4.3 (4.4 in parallel), then 4.5, 4.6, 4.7, 4.8, then 4.9, 4.10, 4.11, and finish with 4.12. One pull request per slice; acceptance criteria first as failing tests marked `@pytest.mark.ac("AC-4.x.y")`.
5. Time-box the ADR-0007 spike to two days. Present the comparison and a recommendation; the choice is recorded in ADR-0007 (status Accepted) before slice 4.2 starts.
6. Keep every Etsy-specific detail behind `MarketplaceReceiptsPort` and the webhook verifier. Nothing above the port may know whether a receipt came from a fixture or from Etsy.
7. Never in this phase: call the real Etsy API or register a webhook; commit an unscrubbed fixture, a token or a buyer's personal data; make a model call from an automation; create a production Supabase project or a `htco-prod` app; write to `orders` outside the intake automation.
8. Stop and ask the owner when: the spike result contradicts the ADR's preference; the fixtures from the Phase 0 spike are missing or cannot be scrubbed; the restore drill misses the RTO or RPO; the worker needs a larger (more expensive) size than approved; an automation specification needs a business decision (for example how to treat a partial refund in the counters).
9. Finish with the [feature-verification](../../../.claude/skills/feature-verification/SKILL.md) run against `htco-dev`, the feature docs, the PLAN status table and the cost actuals.

## Slices

### 4.1 — Job queue spike, port and worker

A time-boxed spike compares the options of ADR-0007 (Supabase Queues, a Postgres task library such as `procrastinate`, an in-house `SKIP LOCKED` table) against its criteria: crash safety, duplicate suppression, dead letter, scheduling, per-shop locks, observability and operational simplicity. Then: the `JobQueue` port in `htco_application` (enqueue with idempotency key, claim with lease, heartbeat, complete, fail with retry policy, dead-letter, schedule) and its adapter in `packages/persistence`; the worker process in `workers/agents` with claim and lease, heartbeat, retries with backoff and jitter, dead-letter state, per-resource locks, graceful shutdown and scheduled jobs; `worker_heartbeats`; the `agent-worker` component added to `infra/digitalocean/` for `htco-dev` with a health port. The worker keeps no local state: everything it needs is in Postgres (master plan Test 15 starts here; it is owned by Phases 6 and 20).

| ID | Acceptance criterion |
|---|---|
| AC-4.1.1 | Killing the worker mid-job leads to exactly one successful completion after restart (automated kill test). |
| AC-4.1.2 | Two workers never run the same job or the same per-shop lock concurrently. |
| AC-4.1.3 | A job that fails N times lands in the dead-letter state with its last error, visible to admins. |
| AC-4.1.4 | SIGTERM lets the current job finish (within a deadline) before exit; a job still running at the deadline is released, not lost. |
| AC-4.1.5 | ADR-0007 is updated to Accepted with the spike results, the measured numbers and the date. |
| AC-4.1.6 | A scheduled job runs once per period even with two worker instances. |
| AC-4.1.7 | Replacing the worker container (a new deploy) loses no job and leaves no state behind: a test starts a fresh worker with an empty filesystem and it continues the queue. |

### 4.2 — Inbox and outbox

`inbox` unique by `(source, event_id)`; `outbox` unique by idempotency key with delivery status, attempts and next attempt time; delivery job. `Idempotency-Key` handling for unsafe API requests: the request hash and response are stored for 24 hours and replayed on retry.

| ID | Acceptance criterion |
|---|---|
| AC-4.2.1 | Inserting the same inbox event twice results in one row and one job. |
| AC-4.2.2 | A crash after the business commit but before delivery results in exactly one delivery after restart. |
| AC-4.2.3 | Repeating a POST with the same `Idempotency-Key` returns the stored response with no second effect; the same key with a different body is rejected. |

### 4.3 — Durable automation engine

Migrations for `automation_definitions` (ID, version, business owner, primary trigger, `callable_from`, retry policy, timeout, side-effect class, approval requirement, readiness, activation phase, specification hash) and `automation_executions` (state machine of `ExecutionState`, attempts, actor kind and ID, trigger kind, correlation and causation IDs, last error, timestamps), unique by `(automation_id, idempotency_key)`. `automation_definitions` rows are loaded from `registry/automations/` by a reviewed sync command (`htco registry sync --automations`, dry-run first, applied by the migration workflow); rows are never edited by hand. `ExecutionStore` adapter on Postgres; each execution is a job on the queue of slice 4.1 (lease, heartbeat, backoff with jitter, dead letter); handlers call use cases and send external effects through the outbox of slice 4.2. Triggers: inbox events, schedules and user actions, all entering through `AutomationEngine.trigger`; user actions come through an API route that authorizes the caller (`POST /v1/automations/{automation_id}/executions`, with `Idempotency-Key`). Read API: `GET /v1/automations` and `GET /v1/automations/{automation_id}/executions`.

| ID | Acceptance criterion |
|---|---|
| AC-4.3.1 | Triggering the same `(automation_id, idempotency_key)` twice, including concurrently from two workers, creates one execution row and runs the handler once; the second call returns the first execution. |
| AC-4.3.2 | An automation whose readiness is not `ready`, whose handler is not deployed, or whose `callable_from` does not include the trigger kind is refused before any execution row is written, and the refusal is logged with the reason. |
| AC-4.3.3 | A user-action trigger from a role without the automation's capability returns 403 (the policy engine decides); a trigger naming a shop the user is not granted returns 404. |
| AC-4.3.4 | A handler that keeps failing follows the definition's retry policy (attempts, backoff bounds) and ends in `DEAD_LETTERED` with its last error; an admin retry is audited and reuses the same execution. |
| AC-4.3.5 | Killing the worker during a `RUNNING` execution leads, after the lease expires, to exactly one `SUCCEEDED` execution and one business effect. |
| AC-4.3.6 | Every execution state change is recorded with the actor (`ActorRef`) and the correlation ID; every transition outside the `ExecutionState` machine is rejected. |
| AC-4.3.7 | `htco registry sync --automations` shows a diff in dry-run, applies it idempotently and records the registry commit it came from; CI fails when the database seed differs from `registry/automations/`. |
| AC-4.3.8 | The engine never calls a model: an automation handler that imports the model gateway or an agent runtime fails an import-linter contract (FR-AUT-04; named model steps arrive with the router in [Phase 8](phase-08-models-tools.md)). |

### 4.4 — Recorded Etsy fixtures and the receipt source port

A fixture set in `tests/fixtures/etsy/`, built from the receipts recorded read-only in the Phase 0 spike (slice 0.3) and scrubbed: no tokens, buyer names, emails or addresses (replaced by obviously synthetic values). Webhook payloads come from recorded deliveries if the webhook portal was available in the spike; otherwise they are built from Etsy's documented schema and marked `synthetic` in the manifest. A `manifest.toml` records, per fixture, its source (`recorded` or `synthetic`), the recording date, the Etsy API version and who scrubbed it. In tests, webhook fixtures are **signed** with a test secret using the signature scheme Etsy documents (verified at kickoff), so the ingress verifies them exactly as it will verify real deliveries.

`MarketplaceReceiptsPort` in `htco_application` (get a receipt with transactions and shipments; list receipts modified since a time, paged) and a `FixtureReceiptSource` adapter in `packages/integrations` that serves the fixture set. The real `EtsyAdapter` of [Phase 9](phase-09-etsy-connection.md) must pass the same contract tests.

The fixture set covers at least: a paid receipt with one item; one item with quantity 3; several items; a cancellation; a full and a partial refund; an update that arrives before an older one; a receipt that never gets a webhook; a malformed payload; a receipt for a shop HTCO does not know.

| ID | Acceptance criterion |
|---|---|
| AC-4.4.1 | Every fixture is listed in `manifest.toml` with source, date, API version and scrubber; a fixture without a manifest entry fails CI. |
| AC-4.4.2 | A test fails if any committed fixture contains an email address, a street address, a person's name from a deny list or a token-shaped string ([TESTING_STRATEGY](../../engineering/TESTING_STRATEGY.md) rule on recorded fixtures). |
| AC-4.4.3 | A contract test suite for `MarketplaceReceiptsPort` runs against `FixtureReceiptSource` and is written so that Phase 9 can run it unchanged against the real adapter with recorded HTTP (respx). |
| AC-4.4.4 | The signing helper used by tests produces signatures that the production verifier of slice 4.5 accepts, and nothing in the production code path reads the test secret. |

### 4.5 — Webhook ingress (signed fixtures)

`POST /webhooks/etsy`: verify the signature on the raw body (with the documented tolerance window), reject a replayed delivery ID, store the event in the inbox, enqueue Etsy Order Intake keyed by `(shop_id, receipt_id)`, and respond 2xx within a second. The webhook secret is read from the environment of `htco-dev` (a test secret in this phase; the real one in Phase 9). The Etsy shop ID in the payload is mapped to an HTCO shop through the shop's marketplace identifier (a seeded development shop in this phase); an unknown shop is stored and ignored, never guessed.

| ID | Acceptance criterion |
|---|---|
| AC-4.5.1 | Invalid signature, modified body, timestamp outside tolerance and a replayed delivery ID are rejected or ignored as documented, and logged without the body. |
| AC-4.5.2 | p95 response time under 1 s with 20 concurrent valid webhooks (load test against the local stack and once against `htco-dev`). |
| AC-4.5.3 | Requests above the per-source rate limit get 429 and are logged; the same delivery retried after the window is accepted. |
| AC-4.5.4 | A webhook for a shop that is not known to HTCO creates no order and one log line with the external shop ID, and no buyer data. |
| AC-4.5.5 | The request body size is limited; an oversized body is rejected before parsing. |

### 4.6 — Receipt ingest

Fetch the receipt through `MarketplaceReceiptsPort`; upsert `orders`, `order_items` and `shipments` in one transaction; apply an update only if the marketplace's `updated_timestamp` is newer; record `order_status_events`; store the raw payload hash; create basic `financial_entries` (revenue, Etsy fees) labeled `ESTIMATED`; emit `orders.ingested.v1` or `orders.updated.v1` through the outbox.

| ID | Acceptance criterion |
|---|---|
| AC-4.6.1 | The same receipt event processed 10 times yields one order, correct item quantities and one `orders.ingested.v1`. |
| AC-4.6.2 | An older update arriving after a newer one does not overwrite newer data. |
| AC-4.6.3 | Units sold and order counts are computed separately and exclude cancelled and refunded orders (property tests). |
| AC-4.6.4 | All money uses `Decimal` with currency; a float anywhere in the money path fails a test. |
| AC-4.6.5 | Every allowed order transition (FR-ORD-03) is tested, each illegal transition is rejected, and every transition records its source and `observed_at`. |
| AC-4.6.6 | Ingest runs under the worker's own database role with an explicit organization and shop filter; a fixture for shop A never writes a row visible to shop B (two-tenant test). |

### 4.7 — Reconciliation

Scheduled job per shop: receipts modified since `cursor − margin`, sorted by update time, paged; nightly 30-day sweep; `sync_cursors`; freshness metric per shop. In this phase it runs against `FixtureReceiptSource` (with a controllable clock); in Phase 9 the same job runs against Etsy.

| ID | Acceptance criterion |
|---|---|
| AC-4.7.1 | With webhooks disabled, every receipt in a 200-receipt fixture set is ingested by reconciliation alone. |
| AC-4.7.2 | A receipt whose webhook was lost is ingested within one reconciliation interval. |
| AC-4.7.3 | The planned daily marketplace call count for one shop is computed in a test from the schedule and stays inside a configured budget (the real Etsy limit is set in Phase 9 from the Phase 0 spike). |
| AC-4.7.4 | A difference between a stored order and the source (status, items or shipments) is recorded in `sync_drifts` with both values and emits `orders.drift_detected.v1`; from [Phase 10](phase-10-fulfillment-incidents.md) it opens an incident (FR-ORD-09). |

### 4.8 — Order automations activated

The order work of slices 4.5 to 4.7 is packaged as the three automations above. The webhook and reconciliation paths trigger Etsy Order Intake (idempotency key `etsy:{shop_id}:{receipt_id}:{event_type}`; verify at kickoff that it also deduplicates a reconciliation hit and a webhook for the same receipt state). Intake triggers Order Deduplication & Verification as a workflow step: it checks that exactly one logical order exists per receipt and that totals and items match the receipt, and records a discrepancy in `sync_drifts` otherwise (an incident from [Phase 10](phase-10-fulfillment-incidents.md), a quality finding for Noa from [Phase 7](phase-07-pilot-office.md)). Paid, cancelled and refunded orders trigger Product Sales Counters, which update unit and order counts per listing and SKU from `order_items.quantity`, net of cancellations and refunds (per product once the catalog model exists in [Phase 12](phase-12-product-studio.md)). Each specification is completed to all 18 sections, with schemas, tests and a recovery runbook.

| ID | Acceptance criterion |
|---|---|
| AC-4.8.1 | The three specifications have no `draft` section; the registry loader accepts `readiness = "ready"` only with every section defined and the tests below passing (the domain refuses otherwise). |
| AC-4.8.2 | **Mandatory Test 1 (fixtures).** One signed `order.paid` fixture for a receipt with two items (quantities 1 and 3) is delivered three times to `POST /webhooks/etsy`, once concurrently and twice in sequence: the result is one order, two order items, 4 units sold, an order count of 1, one `orders.ingested.v1`, one execution per automation and no duplicate outbox message. The test is marked `@pytest.mark.ac("MT-1")` and runs against Postgres in CI. |
| AC-4.8.3 | **Scenario G** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)), against Postgres: one paid receipt replayed N times (N drawn by a property test, including concurrent and out-of-order deliveries through webhook and reconciliation) yields one order, correct item quantities, units sold and order count, one `orders.ingested.v1`, and no duplicate outbox message or other side effect. The in-memory version in `tests/application/test_automation_engine.py` keeps passing. |
| AC-4.8.4 | **Scenario B, part 1**: when the CRM write of Etsy Order Intake fails (database error injected after the inbox insert), the event stays in the inbox, the execution moves to `RETRY_SCHEDULED`, and the idempotent retry produces exactly one order; when retries are exhausted, the execution is dead-lettered and the event is still recoverable from the inbox by an audited admin retry. The final part of Scenario B (Noa finds and verifies the missing record) is in [Phase 7](phase-07-pilot-office.md) at pilot scope and [Phase 14](phase-14-governance.md) at full scope. |
| AC-4.8.5 | Order Deduplication & Verification records one drift for a receipt whose stored totals or items differ from the source, and none for a matching receipt (table-driven tests). |
| AC-4.8.6 | Product Sales Counters never count a cancelled or refunded order, and replaying a cancellation does not decrement twice (property tests). |
| AC-4.8.7 | Each automation has a recovery runbook in `docs/features/orders-ingest/operations.md` and metrics (executions, success rate, retries, dead letters, latency p50 and p95) visible in the queue health view of slice 4.11. |
| AC-4.8.8 | The owner's approval of the three activations is recorded (date, approver, registry commit) in the feature doc. |

### 4.9 — Orders API

`GET /v1/shops/{shop_id}/orders` (filters: status, date range, tracking state; search by receipt number or SKU; cursor pagination); `GET /v1/orders/{order_id}`; buyer fields masked unless the role allows. Contract-first in `openapi.yaml`.

| ID | Acceptance criterion |
|---|---|
| AC-4.9.1 | Cross-tenant and non-granted shop requests return 404. |
| AC-4.9.2 | Viewer responses contain no buyer contact fields. |
| AC-4.9.3 | Every response includes `last_synced_at` for the shop and the source of the data (`fixture` or `etsy`), so a fixture order is never mistaken for a real one. |
| AC-4.9.4 | Every route is in the OpenAPI contract and passes Schemathesis. |

### 4.10 — Orders UI

In `htco-clients`: orders list (filters, search, freshness banner, empty states, safe bulk actions: export selected to CSV with an audit record, and re-sync selected) and order detail (items, shipments, status timeline with sources, financial summary labeled estimated). Built with the components of [Phase 3](phase-03-design-system-web-shell.md) (`OrderTimeline` from the DS0 inventory, slice 3.10). Fixture data is marked as such on screen.

| ID | Acceptance criterion |
|---|---|
| AC-4.10.1 | Data older than the sync window shows a stale banner with the age. |
| AC-4.10.2 | Receipt numbers, SKUs and tracking numbers display LTR inside Hebrew UI. |
| AC-4.10.3 | Keyboard-only: find an order by search and open its detail. |
| AC-4.10.4 | Exporting selected orders produces a CSV with buyer fields masked by role and writes an audit record. |
| AC-4.10.5 | Loading, empty, error, stale and no-permission states match their approved screenshots in he/en × light/dark. |

### 4.11 — Queue and sync health

Metrics for queue depth and age, retries, dead-letter size, worker heartbeat age, webhook lag and sync freshness per shop; alerts when thresholds are crossed (through the error-monitoring service's alert rules until email notifications arrive in [Phase 10](phase-10-fulfillment-incidents.md)); a minimal admin view of dead-lettered jobs and automation executions.

| ID | Acceptance criterion |
|---|---|
| AC-4.11.1 | Stopping the worker beyond the heartbeat threshold sends one alert, and one recovery notice when it returns. |
| AC-4.11.2 | A dead-lettered job appears in the admin view with its last error and can be retried by an admin (audited). |
| AC-4.11.3 | A shop whose last successful sync is older than its window shows as stale in the UI and raises one alert. |

### 4.12 — Restore drill and kill-worker drill

Before any real data exists (Phase 9), the database recovery path is proven. The agent writes the backup facts for the chosen Supabase plan at kickoff (backup frequency, retention, whether point-in-time recovery is enabled; "Supabase Pro backup is not PITR" per the master plan) and proposes RTO and RPO targets; the owner decides them (P4-2). The **restore drill** restores the latest backup of the development project into a scratch project (the supported restore path is verified at kickoff; if the plan cannot restore into a new project, a logical dump of the latest backup is restored instead and the gap is recorded), then verifies row counts per table, RLS with two tenants, the audit log, and that the worker can run the queue against it. The **kill-worker drill** kills the deployed worker during a burst of fixture webhooks in `htco-dev` and checks the result.

| ID | Acceptance criterion |
|---|---|
| AC-4.12.1 | **Mandatory Test 13 (first drill).** The restore drill is run once end to end; the measured restore time and data-loss window are recorded against the owner's RTO and RPO, with date, duration, steps, outcome and follow-ups, in `docs/features/orders-ingest/operations.md` and the platform runbook. A missed target blocks the exit gate. |
| AC-4.12.2 | After the restore, the two-tenant RLS checks and a fixture replay of Test 1 pass against the restored database. |
| AC-4.12.3 | The kill-worker drill in `htco-dev` ends with every fixture order ingested exactly once, and is recorded the same way. |
| AC-4.12.4 | The scratch project is deleted after the drill, and its deletion is recorded (no extra monthly cost left running). |

## Test plan

Domain property tests for order state, counts and money; engine tests against Postgres (replay, concurrency, retry, dead letter, lease expiry) reusing the in-memory engine's test cases; integration tests for queue, inbox, outbox and ingest against the local Supabase stack; webhook security tests with signed fixtures; crash and replay tests (kill tests in CI and the drill in `htco-dev`); contract tests for `MarketplaceReceiptsPort` against the fixture adapter; fixture scrubbing test; API tests and Schemathesis; UI component, screenshot and Playwright tests against MSW and the local stack; mutation testing starts nightly on the order and money modules. Mandatory Test 1 (AC-4.8.2) and Test 13 (AC-4.12.1) are listed in the phase's verification record.

## Security checklist

- [ ] Webhook verification before any processing (ASVS V4, V2.3); request body size limit; replay protection by delivery ID.
- [ ] Webhook secret only in DigitalOcean secrets; the test secret never in production configuration.
- [ ] Workers use their own database role and explicit tenant filters; tested.
- [ ] Buyer PII masked by role; never logged; fixtures scrubbed and checked in CI.
- [ ] Admin retry and export actions audited.
- [ ] Threat model updated for the inbox, outbox and webhook ingress.

## Documentation

`docs/features/job-queue/`, `docs/features/automation-engine/`, `docs/features/orders-ingest/` (with the drill records); the three automation specifications completed in `registry/automations/`; ADR-0007 accepted; ADR-0013 validation log updated with the Postgres results; EVENT_CATALOG entries; DATA_MODEL tables marked created; COST_MODEL with the worker's actual price; ASSUMPTIONS with the RTO and RPO decision.

## Demo

Replay a signed fixture webhook three times and show one order, 4 units and one execution per automation (Test 1); replay it ten times with reconciliation in parallel (Scenario G); inject a failed CRM write and show the event recovered from the inbox (Scenario B, part 1); disable webhooks and show reconciliation catching up; kill the worker mid-burst in `htco-dev` and show it recover; show the restore drill record against the RTO and RPO; browse fixture orders in Hebrew and English.

## Exit gate

- [ ] All AC-4.x pass, including Mandatory Test 1 (fixtures), Scenario G and Scenario B part 1 against Postgres.
- [ ] Mandatory Test 13 drill recorded and inside the owner's RTO and RPO; kill-worker drill recorded.
- [ ] The three order automations are `ready` with fully defined specifications, and the owner approved their activation.
- [ ] ADR-0007 accepted; the worker runs in `htco-dev` with heartbeats and alerts.

## Rollback

Disable webhook processing by flag; set an automation's readiness back to `draft` through the registry sync (new triggers are refused, running executions finish); remove the `agent-worker` component (the queue keeps its jobs); all tables are additive. Fixture orders can be deleted by source (`fixture`) before Phase 9 connects the real shop.

## Risks

| Risk | Mitigation |
|---|---|
| Fixtures differ from what Etsy really sends | Recorded from the owner's shop where possible; manifest marks synthetic ones; Phase 9 runs the same contract tests against real traffic and re-records |
| Etsy's webhook signature scheme differs from the assumption | Verified at kickoff; the verifier is one small module behind the ingress |
| Queue implementation choice delays the phase | Spike time-boxed to two days |
| Owning the engine's durability costs more than expected | The engine stays small (one entry point, one state machine); ADR-0013 can be superseded by an external engine if the spike or drills show gaps |
| Restore drill misses the RTO or RPO | Found before real data; options (the point-in-time recovery add-on of decision D-09, more frequent logical backups) priced and decided by the owner before Phase 9 |

## Cost impact

`agent-worker` component on `htco-dev`, roughly $5–12/month (size and price verified at kickoff). One short-lived scratch Supabase project for the restore drill, deleted afterwards. Automations make no model calls.
