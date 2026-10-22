# Etsy integration — The shop's live data on the dashboard (slice 4.6)

| | |
|---|---|
| **Phase / slice** | Phase 4, slice 4.6 (Shops UI and the dashboard screens that show shop data) |
| **Decision** | Read through, plus a stored order history: the dashboard reads Etsy live through the read-only adapter and a short in-memory cache; Orders and Statistics read the shop's whole order history from HTCO's database once its one-time backfill is complete (owner's decision of 2026-10-22). The API contract stays and only grows. |
| **Research** | [Etsy API capabilities](../../architecture/etsy-api-capabilities.md#in-the-dashboard) |
| **Clients** | `htco-clients`: Shops, Home, Orders, Products, Shipping and Customers screens |
| **Last updated** | 2026-10-22 (stored order history, refunds with reasons) |

## What and why

The owner connected the live shop, but apart from its name the dashboard showed nothing from Etsy, and the shop card still said "Demo". Everything the granted scopes (`shops_r`, `transactions_r`, `listings_r`) or the API key alone allow, and that has a place on a dashboard screen, is now read live and shown. Nothing is written to Etsy and no scope was added.

## Endpoints

All are `GET` (except the manual sync), signed in, for one shop the caller may see (`404` otherwise, as for any shop). Every answer carries `source` (`etsy_live`, or `htco_store` when Orders or Statistics come from the [stored order history](#stored-order-history)), `fetched_at` (when Etsy answered, or the last successful sync) and `stale` (Etsy did not answer and an older cached copy stands in, or no sync succeeded for 30 minutes). Orders and Statistics also carry `sync` (`state`: `syncing_history` or `complete`, `orders_stored`, `last_synced_at`; null when the store is not set up).

| Endpoint | Etsy calls (all GET) | Permission | Screen / panel |
|---|---|---|---|
| `GET /v1/shops/{shop_id}/overview?utc_offset_minutes=` | `getShop`; `getShopReceipts` (`min_created` 31 days ago, newest first, up to 5 pages of 100) | `view_overview` | Shops: the shop card's tiles (orders and revenue today, favourites, rating) and sync freshness. Home: Revenue and Orders today with the change from yesterday; the shop card's 7-day orders and today's numbers |
| `GET /v1/shops/{shop_id}/orders?period=&state=&limit=&offset=` | `getShopReceipts` for the period: `30d` (default; the same cached window as the overview), `90d`, `12m` (twelve calendar months; `min_created` one day earlier than the period) or `all` (no `min_created`); newest first, no status filter, pages of 100 read one after the other up to the period's cap | `view_orders`; buyer name and tracking need `view_buyer_contact` and an unmasked `view_orders` | Orders (live): the period selector, filters with counts, totals, orders per day, the order table. Shipping: awaiting and recently shipped orders |
| `GET /v1/shops/{shop_id}/listings` | `getListingsByShop` (`state=active`, `includes=Images`, up to 3 pages of 100); `getShopSections` | `view_overview` | Products: catalog and listing details. Shops: the "Top listings" strip |
| `GET /v1/shops/{shop_id}/reviews` | `getShop` (average, count); `getReviewsByShop` (newest 100) | `view_overview` | Customers: the "Reviews" strip (replaces the demo "Segments") |
| `GET /v1/shops/{shop_id}/shipping` | `getShopShippingProfiles`, `getShopReturnPolicies`, `getShopProductionPartners` | `view_overview` | Shipping: shipping profiles, return policy and production partners (replace the demo carrier panels) |
| `GET /v1/shops/{shop_id}/stats?utc_offset_minutes=` | `getShop`; the all-time `getShopReceipts` (shared with Orders' All time) until the stored history is complete | `view_overview` (aggregates only, no buyer data) | Shops: the "Statistics" tab per shop; may feed the Home shop card |
| `POST /v1/shops/{shop_id}/order-sync` | None in the request; starts the backfill (from its checkpoint) and the 30-day sync in the background | `request_bulk_resync` with organization scope (Owner, Admin); web app CSRF; audited (`shop.order_sync_requested`, trigger only) | `202` with `sync` |

Problems: `404 not-found`, `409 shop-not-connected` (needs re-authentication or disconnected; no Etsy call is made), `409 etsy-scope-missing` (Etsy answered 403), `503 etsy-data-unavailable` with `Retry-After` (Etsy unreachable or rate limited and nothing recent cached), `503 shop-data-unavailable` (not configured on this server), `422` for an invalid shop ID or query.

How the numbers are made:
- **Sales** count paid orders that were not cancelled or fully refunded, by the day they were placed in the caller's time zone (`utc_offset_minutes`, ±14 hours; the web app sends the browser's offset). Revenue is the order's grand total (items, shipping and tax, before Etsy's fees) in the shop's currency. Profit and fees need the ledger and supplier costs (Phase 13).
- **Order state:** `unpaid` (open or payment processing), `paid`, `shipped` (Etsy says shipped or completed), `canceled`, `refunded` (fully). `overdue`: paid, not shipped, with a physical item, and past the earliest `expected_ship_date` of its items. Partially refunded orders keep their shipping state.
- **Top listings** rank the 30-day window's units per listing. Etsy has no per-listing sales count, and views are listing views, not shop visits.
- **Periods and caps:** the Orders screen reads 30 days (up to 500 orders), 90 days (1,000), 12 months (2,000) or all time (the newest 2,000). The answer carries `period`, `cap` and `window_start` (for all time, when the shop opened). Completed and shipped orders are listed like any other state; Etsy is asked with no `was_paid`, `was_shipped` or `was_canceled` filter.
- **Refunds:** every order lists `refunds` (amount, `refunded_at`, Etsy's `reason` and `status`, and the seller's `note`, the note only for buyer-data roles) and `refunded_total`. Etsy's receipt has no cancellation reason of its own; a cancellation is counted under the reason of its latest refund (null without one).
- `partial` / `truncated` say a window or catalog was larger than one load reads (the period's cap, 300 listings). For orders, `partial` means the cap cut into the period itself: orders from the extra day read for time zones never set it, and when every order read is inside the period, one GET of the first receipt past the cap (cached with the period) decides; if that look fails, `partial` stays true.

Statistics (`/stats`), all labelled in the web app as what they are:
- **From Etsy's shop record:** opened (`created_timestamp`, "open for N years"), total sales (`transaction_sold_count`, Etsy's own count of items sold), reviews (count and average), favourites, active and digital listings, currency.
- **From the order history read** (the newest 2,000 at most; `receipts_read` and `partial` say how many and whether the cap was hit, shown as "based on the latest N orders"): sales (paid, not cancelled or fully refunded), units, gross revenue (grand totals before Etsy's fees, in the shop's currency), average order value (revenue per sale), orders and revenue per month from the first sale's month to this month (in the caller's time zone), the best month by revenue, the top 5 products by units, sales per destination country (country only), and `first_order_at`, the oldest order read; `refunds_and_cancellations`: refunds and cancellations by Etsy's reason (orders and refunded amount).
- **From the stored history only** (null when read live): `customers`, unique buyers among the sales (by the stored name and destination country), and `repeat_customers_percent`, the share of them who bought more than once. From the store, `receipts_read` is the whole history and `partial` is false.

## Caching and rate limits

Etsy's limits are per app (about 10 requests a second and 10,000 a day across every shop). The dashboard never calls Etsy per page view; it reads through `ShopDataCache` (`htco_application.shop_data_cache`), in the API process's memory only:

| Resource | Fresh for | May stand in when Etsy fails, and kept at most |
|---|---|---|
| Shop profile | 5 minutes | 6 hours |
| Listings and sections | 5 minutes | 6 hours (Etsy's limit for listing data) |
| Receipts, 30 days (orders, sales) | 2 minutes | 1 hour (they carry buyer names) |
| Receipts, 90 days, 12 months, all time (each its own entry) | 30 minutes | 1 hour (they carry buyer names) |
| Statistics basis (sale facts of all time: time, totals, items, country; no buyer data) | 6 hours | 24 hours (Etsy's limit for non-listing data) |
| Reviews | 15 minutes | 6 hours |
| Shipping, returns, partners | 30 minutes | 6 hours |

- **Single flight:** concurrent requests for the same shop and resource share one Etsy load. The statistics read the all-time receipts through their own cache entry, so Orders' All time and the Statistics tab share one load.
- **Paging:** a long period reads its pages of 100 one after the other (never in parallel), at most 20 calls a load; the adapter paces every call to the per-second limit and waits out a 429 (`retry-after`, or backoff) before the next. Loads happen only when someone opens the screen and the entry is due.
- **Stale on error:** when Etsy is unreachable, rate limited, the daily budget is held back (`QuotaDeferredError`) or the answer is unreadable, the last copy is served with `stale: true` and its `fetched_at`; the web app says "Etsy is not answering; showing data from …". For 30 seconds after a failure nobody calls Etsy again for that key.
- **Lasting failures** (401 after the refresh, 403 scope) drop the cached copy and answer the problem.
- Each value is dropped by a timer when its `max_stale` ends, even if nobody asks again. Keys carry the connection's generation: a reconnect never sees the previous connection's data. A disconnect drops the shop's cached data at once, and a shop found needing re-authentication has it dropped on the next request.
- Dashboard reads are routine (never `urgent`), so below the daily floor they wait and stale data shows; order intake keeps the budget.
- Tokens: every read uses a token pinned to the connection (`VaultAccessTokenProvider.pinned`), refreshed on demand under the existing per-shop advisory lock (slice 4.4).
- A successful load sets the connection's `last_successful_call_at`, so the shop card's "Last synced" moves.

## Stored order history

The owner decided (2026-10-22) that Orders and Statistics cover everything since the shop opened and load from HTCO's database (`htco_application.order_history`, `htco_persistence.etsy_orders`, migration `20261022120000_etsy_order_history.sql`).

- **Backfill, once per shop:** every receipt since the shop opened, pages of 100 one after the other with no cap, through the read-only adapter (paced, 429s waited out; a 429 that outlasts the adapter's retries is waited out up to 5 more times, then the run stops). Each page is upserted by `(shop_id, receipt_id)` together with the next offset in one transaction: a crash or restart resumes from the checkpoint, and a replay changes nothing. Etsy lists newest first, so orders placed during the backfill only repeat a few rows, never skip one.
- **Incremental:** the last 30 days are read again and upserted (new orders, status changes, shipments, refunds, cancellations). A copy older than the stored one (`updated_timestamp`) is ignored; a newer one replaces the order with its items, refunds and buyer row.
- **Where it runs (until the Phase 5 worker):** reading Orders, Statistics or Overview starts an in-process background task when the backfill is missing or the last sync is older than 10 minutes; the request never waits. A Postgres advisory lock per shop (on a connection of its own) lets one run at a time across instances; the same process does not start another run within 60 seconds. The Owner or an Admin can start one with `POST /order-sync`. A failure records the exception type only.
- **Reads:** while the backfill runs, Orders and Statistics keep the live read and say `sync.state: syncing_history`; once complete they answer `source: htco_store`, every period whole (`partial: false`). If the database cannot be read, the live read stands in.
- **Purge:** a disconnect sets `purge_after` (30 days); `private.purge_disconnected_etsy_orders(now)` (`PostgresOrderStore.purge_disconnected`) deletes the shop's stored orders, items, refunds, buyer rows and sync state after it. The Phase 5 worker schedules it.

### Data retention

| Data | Stored? | Where | Who may read it | Why |
|---|---|---|---|---|
| Receipt: ID, status, paid/shipped, totals (exact amount/divisor/currency), created/updated times, destination country | Yes | `app.etsy_orders` | Members who may see the shop | Orders screen and statistics over the whole history |
| Items: listing ID, title, SKU, quantity, unit price, digital, expected ship date (ship-by) | Yes | `app.etsy_order_items` | Members who may see the shop | Orders screen, top products, overdue |
| Shipments: carrier and time | Yes | `app.etsy_orders.shipments` | Members who may see the shop | Shipping state |
| Refunds: amount, date, Etsy's reason and status | Yes | `app.etsy_order_refunds` | Members who may see the shop | Refunds and cancellations by reason |
| Refund note from the seller (`note_from_issuer`) | Yes | `app.etsy_order_refunds` | Answered only to buyer-data roles; never logged | Context for the reason |
| Buyer's name, shipping address (lines, city, state, postal code, country), tracking numbers | Yes | `app.etsy_order_buyers` | Owner, Admin; Operations and Support with access (RLS and the API) | Order statistics (unique and repeat buyers) and later insights; never logged, reported or audited |
| Buyer's email, phone, messages, gift message, notes | **No** | Never read by the adapter | — | Contact data is not needed; no column can hold it |
| Payments, fees, ledger | No | — | — | Phase 13 |

## Personal data

- **Read minimally.** The adapter maps a receipt to `OrderReceipt` and drops everything else at the boundary: no email, phone, messages, gift text or notes. The buyer's name and shipping address are kept for the stored history (see the table above) and excluded from every `repr`; the Orders screen shows only a display name (first name and last initial, "Dana L.").
- **Statistics are aggregates.** Their cached basis is reduced to sale facts (time, totals, items, country) with no name or tracking number, which is why it may live longer than the receipts; the answer holds counts and sums only.
- **Shown to few.** The display name and tracking numbers are answered only to Owner, Admin, Operations and Support (`view_buyer_contact` and unmasked `view_orders`). Finance, Developer and Viewer get the order without them; the answer says `buyer_details: false`.
- **Never logged.** `OrderReceipt.buyer_display_name`, `OrderShipment.tracking_code` and `ShopReview.text` are excluded from `repr`; nothing logs, audits or raises with them; errors name the operation and status only. Tests capture the logs and search for the fake markers.
- **Stored as the table above says**, and nothing else; the live in-memory copy lives at most one hour for receipts. Order answers are `Cache-Control: no-store`; the others `private, no-cache`. Database errors are logged by type only (a message can quote a row).
- Review text is the buyer's public review on Etsy; it is shown to members who may see the shop and kept out of logs like order data.

## Read only

Every new adapter method goes through the guarded `_request` (GET only, before any token is fetched) and the client's request hook; a test calls each one against a strict mock that fails on any other method, and the source scan still finds no `allow_write=True`.

## Phase 5

The stored history already answers `source: "htco_store"` for Orders and Statistics. Phase 5 moves the syncs from the API process to the worker's queue (and schedules the purge), adds webhooks for near-real-time intake, and may serve the Overview's sales from the store too; the API contract stays.

## Deferred, with the phase

| What | Why not now | Phase |
|---|---|---|
| Webhooks, syncs on the worker's queue, the scheduled purge | Needs the worker and webhook signing | 5 |
| Late-shipment incidents and alerts, SLA guard, tracking upload (`createReceiptShipment`) | Incidents and any write to Etsy need the incident engine and the owner's approval | 6 |
| Payments and ledger (`getPayments`, `getShopPaymentAccountLedgerEntries`) | Allowed by `transactions_r`, but raw ledger lines mislead without fee classification, payout matching and supplier costs | 13 |
| Customer list and segments | The stored history gives unique and repeat buyer counts; a customer list and segments need the CRM | 14 (CRM) |
| Listing health (missing attributes, expiring listings), inventory and variations | Phase 11 audits; inventory needs `getListingInventory` per listing | 11 |
| Shop stats (visits, conversion, search terms), messages, coupons, ads | Not in Etsy's API | — (CSV upload, Phase 14 for messages) |
| Review replies, any edit of a listing, price or stock | Writes; Etsy has no review-reply API | 12 (Product Studio, with approval) |
