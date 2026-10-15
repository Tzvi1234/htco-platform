# Etsy Open API v3: what HTCO can do with it

| | |
|---|---|
| **Researched** | 2026-10-15, from the live OpenAPI spec (`etsy.com/openapi/generated/oas/3.0.0.json`), developers.etsy.com and the etsy/open-api announcements |
| **Related** | [ADR-0008](../adr/0008-etsy-integration.md), [Multi-shop policy](etsy-multi-shop-policy.md), Phases 4, 5, 6, 11, 12, 13 |

The owner's goal: once a shop is connected, HTCO detects orders immediately, acts on them automatically, shows statistics, and offers every feature the API allows. This page is the inventory. Anything not confirmed is marked **UNCONFIRMED**.

## Headline facts

- **Webhooks exist and work for personal apps too** (since 2026-02-17). There are 11 events: `order.paid`, `order.canceled`, `order.shipped`, `order.delivered`, `listing.created/updated/deleted`, and `inventory.created/updated/deleted/out_of_stock`. Order detection is therefore push-based. A polling backstop reconciles every 5–15 minutes, because delivery order and exactly-once delivery are not guaranteed.
- **Israel restriction (2026-07-20, discussion #1646).** It affects new personal keys and new sellers on commercial apps in Israel, India and Vietnam:
  - `createReceiptShipment` (tracking upload) is blocked;
  - buyer address fields on receipts are hidden.

  The exceptions are Etsy Preferred Partners and approved categories (Fulfillment or Print-On-Demand). **We must test this with our own key before building the fulfillment flow on it.** The fallback is that tracking and addresses flow through an approved POD or fulfillment partner (for example Printify or Printful), or a manual task.
- **There is no API for** shop stats (views, visits, conversion, search terms), conversations or messages, coupons and sales, Etsy Ads, or review replies.
- **Rate limits are per app, not per shop.** Commonly 10 QPS and 10,000 QPD (UNCONFIRMED; the real figures are in the Developer Portal). They can be raised on request.

## Capabilities by area

| Area | Read | Write | Scopes | HTCO use |
|---|---|---|---|---|
| Shop | shop, owner, counts (`review_average`, `num_favorers`, `transaction_sold_count`, `listing_active_count`, vacation) | `updateShop` (title, announcement, sale messages, vacation) | shops_r/w | Shop overview, vacation switch |
| Sections | sections, listings per section | create, update, delete | shops_w | Catalog structure |
| Listings | by shop with state filter (draft/active/inactive/expired/sold_out) and includes (images, inventory, shipping, personalization, translations, videos); batch of 100 | create draft, update, activate, delete | listings_r/w/d | Listing Health (Phase 11), Product Studio (Phase 12) |
| Inventory | per listing, batch of 100, products and offerings | `updateListingInventory`: price, quantity, SKU, readiness per product; up to 3 variations | listings_r/w | Price and stock sync, out-of-stock reactions |
| Media | images (up to 20), variation images, videos (several), digital files | upload, delete | listings_w | Studio, health checks |
| Content | translations, properties (attributes), personalization (multi-question, add-on pricing), EU ECGT fields | create, update, delete | listings_w | Health checks: attributes and taxonomy completeness |
| Shipping | profiles, destinations, upgrades, carriers | create, update, delete | shops_r/w | Templates across shops |
| Policies | return policies, processing profiles (readiness states), holiday preferences | create, update, delete, consolidate | shops_r/w | Governance |
| Orders | receipts filtered by created or last-modified, paid, shipped, delivered, canceled; transactions (SKU, variations, `expected_ship_date`, coupons) | `updateShopReceipt` (was_shipped, was_paid); `createReceiptShipment` (tracking; **restricted**, see above) | transactions_r/w | Order intake (Phase 5), fulfillment and incidents (Phase 6) |
| Money | ledger entries (payments, fees, refunds, disbursements, ads debits…), payments (gross, fees, net, adjustments), payment by receipt | — | transactions_r | P&L and reconciliation (Phases 10, 13) |
| Reviews | by shop and by listing, with date filters | — (no replies) | none | Review monitoring and alerts |
| Taxonomy | seller and buyer nodes, properties per node | — | none | Health checks |
| Users | `getMe`, user, addresses | delete address | shops_r, email_r, address_r | Onboarding |

Notes:
- Featured listings are read-only since 2026-10-14.
- `buyer_email` and `primary_email` need separate, case-by-case permission.
- Etsy does not sell shipping labels through the API.

## What HTCO builds on it

1. **Instant order intake.** `order.paid` arrives by webhook, with the signature verified (Standard Webhooks: `webhook-id`, `webhook-timestamp`, `webhook-signature`, HMAC-SHA256, 5-minute window). It is deduplicated on `webhook-id` and receipt ID. HTCO then fetches the receipt and its transactions, routes each line to its supplier by SKU, and runs the polling backstop on `min_last_modified`.
2. **SLA guard.** A task opens when `expected_ship_date` nears without a shipment. `order.canceled` stops the supplier order. `order.shipped` and `order.delivered` close the loop.
3. **Tracking upload** through `createReceiptShipment` where permitted. Otherwise a partner integration, or a task for the owner.
4. **Inventory and price sync** from supplier stock and cost. Reacts to `inventory.out_of_stock` and `inventory.updated`; nightly reconciliation uses the batch reads.
5. **Listing Health.**
   - A nightly audit checks tags, title, images and video, required attributes, return and processing profiles, ECGT fields, expiring listings and states.
   - `listing.updated` catches unintended edits.
6. **Product Studio.** Draft, then media, properties, inventory and translations, then activation after the owner approves.
7. **Review monitoring.** Hourly pulls; ratings of 3 or below raise an alert, and `review_average` is tracked over time.
8. **Finance.** A daily ledger pull is classified by type and joined to payments and supplier COGS, giving profit per order and per shop. Disbursements are matched to bank deposits.
9. **Statistics on the overview.** Orders, revenue, average order value, profit, late shipments, review average, out-of-stock items, and favourites as a demand proxy, all from our own data. Views, visits and conversion are not in the API. They can come only from a CSV export from Shop Manager that the owner uploads. **Scraping is not allowed.**
10. **Messages** have no API. Etsy's notification emails can be read from the owner's mailbox (Phase 14).
11. **Token hygiene.** Access tokens are refreshed hourly; the owner re-consents before the 90-day refresh token expires. The shared secret is rotated.

## Rules we keep (API Terms, 2026-08-18)

- One app (one keystring) for all shops. Never extra keys or apps to get around limits or review.
- Minimum data, minimal caching, and no data shown older than 6 hours (listings) or 24 hours (other data).
- Report a breach within 24 hours.
- Show the trademark notice: "The term 'Etsy' is a trademark of Etsy, Inc." and that HTCO is not endorsed by Etsy.
- No scraping, and no messages to buyers through the API about orders or shipping.
- No training AI on Etsy content. AI drafts are approved by a human before they reach Etsy.

## Open questions (test with our own key in Phase 4)

- Do `createReceiptShipment` and the receipt addresses work for an Israeli shop on our key?
- Which tier do we get, and what are its rate limits and webhook access?
- Does `updateShopReceipt` (`was_shipped`) still work where tracking is blocked?
