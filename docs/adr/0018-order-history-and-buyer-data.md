# ADR-0018: Order history is stored with the buyer's name and shipping address, never contact data

| | |
|---|---|
| **Status** | Accepted (owner decision, 2026-10-22, after reading Etsy's API policy; corrected the same day) |
| **Date** | 2026-10-22 |
| **Related** | [ADR-0008](0008-etsy-integration.md) (Etsy integration), [Phase 5](../plan/phases/phase-05-orders-ingest.md) slices 5.3 to 5.5, [DATA_MODEL §5–6](../architecture/DATA_MODEL.md#5-personal-data-classification), assumption A-09 |

## Context

Phase 5 ingests the shop's orders. Etsy's policy, as the owner read it, allows an app to store a seller's **order history** and to compute shop statistics from it. It does not allow keeping **customer lists for marketing**, or **permanent product lists for copying to other shops**. The dashboard's live read-through (slice 4.6) is capped and slow for long periods; statistics need the full history.

## Decision drivers

- Stay inside Etsy's policy and keep personal data to what statistics need (ER-DATA-07).
- Instant Orders and Statistics screens over the whole history, without Etsy's caps.
- Idempotent, rate-limited sync that recovers from missed events (ER-REL-01).

## Decision

1. **Backfill, once per shop:** page through every receipt since the shop opened, sequentially and inside the rate-limit budget, and store the orders permanently.
2. **Ongoing sync:** every reconciliation run (scheduled, and on demand when a dashboard screen opens while the last sync is stale) re-reads the **last 30 days** and upserts new or changed receipts idempotently (status changes, shipments, refunds); a nightly sweep runs as well. Etsy webhooks are added if the Seller App offers them (slice 5.3); reconciliation stays on either way.
3. **What is stored** (per order): items (listing ID, title, quantity, price), totals and currency, states and their timestamps, ship-by date, shipments with carrier and status, and the buyer's **name and shipping address** (DC1, for statistics such as repeat buyers and destinations; masked by role in every response and never logged).
4. **Contact data is never stored:** no buyer email, phone or messages. Anything of that kind a screen ever needs is fetched live through the read-through cache (slice 4.6, in memory, short-lived) and merged at read time.
5. **Dashboard reads:** once a shop is ingested, Orders and Statistics read from the database; the live read-through stays for the freshest window and any live-only field.
6. **No marketing customer lists:** no stored customer list, segments or marketing profiles. A per-buyer view, if ever needed, is computed from the order history on demand, not kept as a list.
7. **Delivery:** the order history store (backfill and 30-day re-read, tables such as `etsy_orders`, `etsy_order_items`, `etsy_order_sync_state`) ships first as in-process API background work with a per-shop advisory lock. Slices 5.4 and 5.5 move its triggering onto the job queue and the durable engine (Etsy Order Intake); they do not re-implement it.

### Retention

| Data | Stored? | Why |
|---|---|---|
| Orders: receipt ID, states and timestamps, totals, currency, ship-by | Yes, while the shop is connected | Order history and statistics (allowed) |
| Items: listing ID, title, quantity, price | Yes | Units sold, revenue per listing |
| Shipments: carrier, status, tracking number, timestamps | Yes | Fulfillment state and SLAs |
| Buyer name and shipping address | Yes (DC1), masked by role, never logged | Statistics (owner decision); retention per DATA_MODEL §6 |
| Buyer email, phone, messages | **Never** | Contact data; the marketing-list prohibition |
| Customer lists, segments, marketing profiles | **Never** | Prohibited by Etsy's policy |
| Copies of listings for use in other shops | **Never** | Prohibited; item titles stay as the order's line text |

### Deletion path

Disconnecting a shop deletes its tokens at once and sets `purge_after` 30 days later (slice 4.7). The daily purge job (`maintenance.purge_disconnected_shops`, slice 5.1) then deletes the shop's synced data in one audited transaction; the order history tables join its purge list when slices 5.4 and 5.5 take the store over. Records under legal bookkeeping retention (financial entries, DATA_MODEL §6) are kept with the buyer's name and address masked. Reconnecting before the purge date keeps the data.

## Consequences

- Positive: statistics are instant over the full history; no contact data at rest.
- Negative: buyer names and addresses are DC1 at rest, so masking by role (slice 5.6), the restore drill and the purge must cover them; the backfill costs Etsy requests once per shop (budgeted in slice 5.5, AC-5.5.3).
- Follow-ups: the ingest's mapper and tests prove that no email, phone or message field reaches the database.

## Validation

Slice 5.4 tests: a receipt fixture with fake contact values is ingested and no stored row holds them; slice 5.5 tests: the backfill and the 30-day window stay inside the daily rate-limit budget.

## Validation log

- 2026-10-22 — Decision recorded; corrected the same day (name and shipping address stored; contact data never).
