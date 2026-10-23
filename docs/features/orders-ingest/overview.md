# Orders ingest — Overview

| | |
|---|---|
| **Slug** | `orders-ingest` |
| **Phase / slices** | Phase 5 — slice 5.3 (webhook ingress for all 11 Etsy events); 5.4 (receipt ingest on the order history store) and 5.5 (reconciliation on the queue) follow |
| **Requirements** | [FR-ORD-01, FR-ORD-02](../../product/PRD.md), ER-REL-01 |
| **Decisions** | [ADR-0018](../../adr/0018-order-history-and-buyer-data.md) (order history and buyer data), [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (engine and gateway) |
| **Status** | 5.3 built behind `HTCO_ETSY_WEBHOOKS_ENABLED` (off); the owner registers the endpoint and its secret ([operations](operations.md#registering-the-webhook-in-the-etsy-developer-portal)) |
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

## Out of scope (this slice)

- Writing the order into the store (5.4: Etsy Order Intake's handler) and reconciliation (5.5).
- A listing cache: listing and stock events publish what the re-read shows; Listing Health (Phase 11) and price and stock monitoring (Phase 13) consume them.
- Activating Etsy Order Intake (5.11, the owner's approval): until then the engine refuses it, the refusal is logged, and the order arrives through reconciliation.
