# Orders ingest — Overview

| | |
|---|---|
| **Slug** | `orders-ingest` |
| **Phase / slices** | Phase 5 — slice 5.3 (webhook ingress for all 11 Etsy events); 5.4 (receipt ingest on the order history store) and 5.5 (reconciliation on the queue) follow |
| **Requirements** | [FR-ORD-01, FR-ORD-02](../../product/PRD.md), ER-REL-01 |
| **Decisions** | [ADR-0018](../../adr/0018-order-history-and-buyer-data.md) (order history and buyer data), [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (engine and gateway) |
| **Status** | 5.3 merged (#69) behind `HTCO_ETSY_WEBHOOKS_ENABLED` (off); the owner registers the endpoint and its secret ([operations](operations.md#registering-the-webhook-in-the-etsy-developer-portal)). 5.4 built: Etsy Order Intake's handler on the engine, deployed in the worker, inactive until the owner activates it (5.11) |
| **Last updated** | 2026-10-24 |

## What and why

Every paid Etsy order must reach HTCO exactly once, quickly, whether or not Etsy's webhook arrives. Webhooks (this slice) make it near real time: Etsy calls `POST /webhooks/etsy` when an order, a listing or its stock changes. HTCO verifies the signature, keeps the event once in the inbox, and a worker job re-reads what the event names through the guarded, GET-only adapter. The payload is never trusted on its own. Reconciliation (5.5) stays on whatever webhooks do.

## Users and actors

- **Etsy** calls the endpoint (Standard Webhooks signature).
- **The `agent-worker`** runs the `etsy.webhook_event` job: order events start Etsy Order Intake on the durable engine; listing and stock events re-read Etsy and publish `catalog.*` events.
- **The owner** registers the endpoint in the Etsy developer portal and stores the signing secret.

## Acceptance criteria

| ID | Criterion |
|---|---|
| AC-5.3.1 | Invalid signature, modified body, timestamp outside tolerance and replayed `webhook-id` are rejected or ignored as documented, and logged without the body. |
| AC-5.3.2 | p95 response time under 1 s with 20 concurrent valid webhooks. |
| AC-5.3.3 | Requests above the per-source rate limit get 429 and are logged; Etsy's retries are accepted after the window. |
| AC-5.3.4 | Each of the 11 event types is routed as in the table (table-driven test with recorded payloads); an unknown type is stored with no job. |
| AC-5.4.1 | The same receipt event processed 10 times yields one order, correct item quantities and one `orders.ingested.v1`. |
| AC-5.4.2 | An older update arriving after a newer one does not overwrite newer data. |
| AC-5.4.3 | Units sold and order counts are computed separately and exclude cancelled and refunded orders (property tests). |
| AC-5.4.4 | All money uses `Decimal` with currency; a float anywhere in the money path fails a test. |
| AC-5.4.5 | Every allowed order transition (FR-ORD-03) is tested, each illegal transition is rejected, and every transition records its source and `observed_at`. |
| AC-5.4.6 | Webhook ingest and the #57 sync write through the same upsert; no second order table exists (schema inventory test). A receipt fixture with fake contact values stores none of them (ADR-0018). |

## Etsy Order Intake (slice 5.4)

The `etsy-order-intake` automation's handler runs on the worker's durable engine. Its Etsy reads go through the Action Gateway (`etsy.receipts.read`, the API channel's order history form). Its writes go to the #57 store through one ingest path shared with the backfill and the 30-day sync: the order with its items, refunds and buyer row; the order state and every transition (`app.order_status_events`); estimated revenue and refund lines (`app.financial_entries`); and the `orders.*` events. A webhook's receipt and a requested history run (the dashboard's stale read, the Owner's manual sync) are its two kinds of run. The API process keeps its own sync only as the fallback behind `HTCO_ORDER_SYNC_IN_API` (on until 5.11).

## Out of scope (this slice)

- Reconciliation, the nightly sweep, drifts (5.5).
- Etsy fees: the receipt carries none; fee lines come with the payment ledger (Phase 13).
- A listing cache: listing and stock events publish what the re-read shows; Listing Health (Phase 11) and price and stock monitoring (Phase 13) consume them.
- Activating Etsy Order Intake (5.11, the owner's approval): until then the engine refuses it, the refusal is logged, and the order arrives through reconciliation.
