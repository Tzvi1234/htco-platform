# Etsy integration — The shop's live data on the dashboard (slice 4.6)

| | |
|---|---|
| **Phase / slice** | Phase 4, slice 4.6 (Shops UI and the dashboard screens that show shop data) |
| **Decision** | Read through, not ingested: the dashboard reads Etsy live through the read-only adapter and a short in-memory cache. Phase 5 ingests orders exactly once into HTCO's own store and then swaps the orders source; the API contract stays. |
| **Research** | [Etsy API capabilities](../../architecture/etsy-api-capabilities.md#in-the-dashboard) |
| **Clients** | `htco-clients`: Shops, Home, Orders, Products, Shipping and Customers screens |
| **Last updated** | 2026-10-22 |

## What and why

The owner connected the live shop, but apart from its name the dashboard showed nothing from Etsy, and the shop card still said "Demo". Everything the granted scopes (`shops_r`, `transactions_r`, `listings_r`) or the API key alone allow, and that has a place on a dashboard screen, is now read live and shown. Nothing is written to Etsy and no scope was added.

## Endpoints

All are `GET`, signed in, for one shop the caller may see (`404` otherwise, as for any shop). Every answer carries `source` (`etsy_live`), `fetched_at` (when Etsy answered) and `stale` (Etsy did not answer and an older cached copy stands in).

| Endpoint | Etsy calls (all GET) | Permission | Screen / panel |
|---|---|---|---|
| `GET /v1/shops/{shop_id}/overview?utc_offset_minutes=` | `getShop`; `getShopReceipts` (`min_created` 31 days ago, newest first, up to 5 pages of 100) | `view_overview` | Shops: the shop card's tiles (orders and revenue today, favourites, rating) and sync freshness. Home: Revenue and Orders today with the change from yesterday; the shop card's 7-day orders and today's numbers |
| `GET /v1/shops/{shop_id}/orders?state=&limit=&offset=` | `getShopReceipts` (the same cached 30-day window) | `view_orders`; buyer name and tracking need `view_buyer_contact` and an unmasked `view_orders` | Orders (live): filters with counts, totals, orders per day, the order table. Shipping: awaiting and recently shipped orders |
| `GET /v1/shops/{shop_id}/listings` | `getListingsByShop` (`state=active`, `includes=Images`, up to 3 pages of 100); `getShopSections` | `view_overview` | Products: catalog and listing details. Shops: the "Top listings" strip |
| `GET /v1/shops/{shop_id}/reviews` | `getShop` (average, count); `getReviewsByShop` (newest 100) | `view_overview` | Customers: the "Reviews" strip (replaces the demo "Segments") |
| `GET /v1/shops/{shop_id}/shipping` | `getShopShippingProfiles`, `getShopReturnPolicies`, `getShopProductionPartners` | `view_overview` | Shipping: shipping profiles, return policy and production partners (replace the demo carrier panels) |

Problems: `404 not-found`, `409 shop-not-connected` (needs re-authentication or disconnected; no Etsy call is made), `409 etsy-scope-missing` (Etsy answered 403), `503 etsy-data-unavailable` with `Retry-After` (Etsy unreachable or rate limited and nothing recent cached), `503 shop-data-unavailable` (not configured on this server), `422` for an invalid shop ID or query.

How the numbers are made:
- **Sales** count paid orders that were not cancelled or fully refunded, by the day they were placed in the caller's time zone (`utc_offset_minutes`, ±14 hours; the web app sends the browser's offset). Revenue is the order's grand total (items, shipping and tax, before Etsy's fees) in the shop's currency. Profit and fees need the ledger and supplier costs (Phase 13).
- **Order state:** `unpaid` (open or payment processing), `paid`, `shipped` (Etsy says shipped or completed), `canceled`, `refunded` (fully). `overdue`: paid, not shipped, with a physical item, and past the earliest `expected_ship_date` of its items. Partially refunded orders keep their shipping state.
- **Top listings** rank the 30-day window's units per listing. Etsy has no per-listing sales count, and views are listing views, not shop visits.
- `partial` / `truncated` say a window or catalog was larger than one load reads (500 orders, 300 listings).

## Caching and rate limits

Etsy's limits are per app (about 10 requests a second and 10,000 a day across every shop). The dashboard never calls Etsy per page view; it reads through `ShopDataCache` (`htco_application.shop_data_cache`), in the API process's memory only:

| Resource | Fresh for | May stand in when Etsy fails, and kept at most |
|---|---|---|
| Shop profile | 5 minutes | 6 hours |
| Listings and sections | 5 minutes | 6 hours (Etsy's limit for listing data) |
| Receipts (orders, sales) | 2 minutes | 1 hour (they carry buyer names) |
| Reviews | 15 minutes | 6 hours |
| Shipping, returns, partners | 30 minutes | 6 hours |

- **Single flight:** concurrent requests for the same shop and resource share one Etsy load.
- **Stale on error:** when Etsy is unreachable, rate limited, the daily budget is held back (`QuotaDeferredError`) or the answer is unreadable, the last copy is served with `stale: true` and its `fetched_at`; the web app says "Etsy is not answering; showing data from …". For 30 seconds after a failure nobody calls Etsy again for that key.
- **Lasting failures** (401 after the refresh, 403 scope) drop the cached copy and answer the problem.
- Each value is dropped by a timer when its `max_stale` ends, even if nobody asks again. Keys carry the connection's generation: a reconnect never sees the previous connection's data. A disconnect drops the shop's cached data at once, and a shop found needing re-authentication has it dropped on the next request.
- Dashboard reads are routine (never `urgent`), so below the daily floor they wait and stale data shows; order intake keeps the budget.
- Tokens: every read uses a token pinned to the connection (`VaultAccessTokenProvider.pinned`), refreshed on demand under the existing per-shop advisory lock (slice 4.4).
- A successful load sets the connection's `last_successful_call_at`, so the shop card's "Last synced" moves.

## Personal data

- **Read minimally.** The adapter maps a receipt to `OrderReceipt` and drops everything else at the boundary: no email, no street address, no messages, no gift text. The buyer's name becomes a display name (first name and last initial, "Dana L.") before it reaches the application.
- **Shown to few.** The display name and tracking numbers are answered only to Owner, Admin, Operations and Support (`view_buyer_contact` and unmasked `view_orders`). Finance, Developer and Viewer get the order without them; the answer says `buyer_details: false`.
- **Never logged.** `OrderReceipt.buyer_display_name`, `OrderShipment.tracking_code` and `ShopReview.text` are excluded from `repr`; nothing logs, audits or raises with them; errors name the operation and status only. Tests capture the logs and search for the fake markers.
- **Not stored.** Nothing is written to the database; the in-memory copy lives at most one hour for receipts. Order answers are `Cache-Control: no-store`; the others `private, no-cache`.
- Review text is the buyer's public review on Etsy; it is shown to members who may see the shop and kept out of logs like order data.

## Read only

Every new adapter method goes through the guarded `_request` (GET only, before any token is fetched) and the client's request hook; a test calls each one against a strict mock that fails on any other method, and the source scan still finds no `allow_write=True`.

## Phase 5 swap

The answers name their `source`. When Phase 5 ingests orders exactly once (webhooks plus the polling backstop), `/orders` and the sales in `/overview` read the store and answer `source: "htco_store"` with the same fields; the web app needs no change beyond showing the new source label. The 30-day window and the 500-order cap go away then.

## Deferred, with the phase

| What | Why not now | Phase |
|---|---|---|
| Exactly-once order ingest, webhooks, polling backstop, order history beyond 30 days | Needs the durable store, the worker and webhook signing | 5 |
| Late-shipment incidents and alerts, SLA guard, tracking upload (`createReceiptShipment`) | Incidents and any write to Etsy need the incident engine and the owner's approval | 6 |
| Payments and ledger (`getPayments`, `getShopPaymentAccountLedgerEntries`) | Allowed by `transactions_r`, but raw ledger lines mislead without fee classification, payout matching and supplier costs | 13 |
| Customer list and segments (repeat buyers, countries) | Needs a customer store built from ingested orders; aggregating buyer data live is not minimal | 5 (store), 14 (CRM) |
| Listing health (missing attributes, expiring listings), inventory and variations | Phase 11 audits; inventory needs `getListingInventory` per listing | 11 |
| Shop stats (visits, conversion, search terms), messages, coupons, ads | Not in Etsy's API | — (CSV upload, Phase 14 for messages) |
| Review replies, any edit of a listing, price or stock | Writes; Etsy has no review-reply API | 12 (Product Studio, with approval) |
