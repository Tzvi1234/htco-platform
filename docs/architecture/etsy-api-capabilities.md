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

## In the dashboard

Phase 4 (slice 4.6) shows every read our granted scopes (`shops_r`, `transactions_r`, `listings_r`) or the API key allow where a dashboard screen has a place for it, live through the read-only adapter and a short cache ([dashboard data](../features/etsy-integration/dashboard-data.md)). Nothing is written and no scope was added.

| Etsy endpoint | Scope | Screen / panel | Now or deferred |
|---|---|---|---|
| `getShop` | API key | Shops: shop card tiles (favourites, rating) and freshness, the Statistics tab (opened, total sales, reviews, favourites, listings); Home: shop card; Customers: review average and count | Now |
| `getShopReceipts` (30 days, newest first) | `transactions_r` | Orders (live view, default period); Home: Revenue and Orders today, 7-day orders; Shops: orders and revenue today; Shipping: awaiting and shipped orders | Now (read-through); exactly-once ingest and webhooks in Phase 5 |
| `getShopReceipts` (90 days, 12 months, all time; pages of 100 in turn, up to 1,000 or 2,000) | `transactions_r` | Orders: the period selector; Shops: the Statistics tab (revenue, orders per month, best month, top products, countries) | Now (read-through, capped); the full history from the Phase 5 store |
| Receipt `transactions` (embedded) | `transactions_r` | Orders: items, ship-by dates; Home: top listings by units | Now |
| Receipt `shipments` (embedded) | `transactions_r` | Orders and Shipping: carrier, shipped date; tracking number for buyer-contact roles | Now |
| `getListingsByShop` (`active`, `includes=Images`) | `listings_r` | Products: catalog and details; Shops: top listings strip | Now |
| `getShopSections` | API key | Products: section names and filter | Now |
| `getReviewsByShop` | API key | Customers: "Reviews" strip (replaces the demo segments) | Now |
| `getShopShippingProfiles` | `shops_r` | Shipping: profiles, destinations, costs, delivery days (replace the demo carrier panels) | Now |
| `getShopReturnPolicies` | API key | Shipping: return policy | Now |
| `getShopProductionPartners` | `shops_r` | Shipping: production partners | Now |
| `getShopReceiptTransactionsByShop` | `transactions_r` | — (the receipts already embed their transactions) | Not needed |
| `getShopReadinessStateDefinitions` (processing profiles) | `shops_r` | Shipping: processing times | Deferred to Phase 11 (listing health), with per-listing readiness |
| `getListingInventory`, variations, SKUs per product | `listings_r` | Products: stock per variation | Deferred to Phase 11 (one call per listing) |
| `getPayments`, `getShopPaymentAccountLedgerEntries` | `transactions_r` | Finance overview and ledger | Deferred to Phase 13: allowed by our scope, but meaningful only with fee classification, payout matching and supplier costs |
| Webhooks (`order.*`, `listing.*`, `inventory.*`) | app setting | Live updates instead of the 2-minute cache | Phase 5 |
| `updateShopReceipt`, `createReceiptShipment`, any listing or shop write | `*_w` | — | As tools with approval policies W1–W3, owner-trigger first ([full inventory](#full-endpoint-inventory)); Phases 6, 7, 11, 12, 13 |
| Buyer email and address (`buyer_email`, `first_line` …) | `transactions_r` (address hidden for Israeli keys) | — | Not read: only a display name (first name, last initial) for Owner, Admin, Operations and Support, and the destination country (`country_iso`) for the statistics' aggregate countries mix |

## Full endpoint inventory

Added 2026-10-23 for the owner's "API first" instruction ([ADR-0022](../adr/0022-etsy-write-tools-and-browser-scope.md)). The source is the live OpenAPI spec (`etsy.com/openapi/generated/oas/3.0.0.json`, fetched 2026-10-23): it lists **104 operations**, every one of which appears below. Each operation maps to one of 38 draft tools in [`registry/tools/etsy_open_api.toml`](../../registry/tools/etsy_open_api.toml): 20 read tools and 18 write tools. Tools are named `etsy.<area>.<action>`, and reads and writes are separate tools (ADR-0014). The "Granted to" column lists the draft grants in `registry/employees/`, which are confirmed or removed at each employee's activation. "Automations only" means no employee holds the tool; automations and the owner use it through the Action Gateway.

**Approval policies**

| Policy | Meaning |
|---|---|
| **R0** | Read, no buyer data. No approval |
| **R1** | Read with buyer, payment or account data: masked by role, never logged; audited where personal |
| **W1** | Write: an approval decided by the Owner or an Admin, outbox with idempotency key, re-read verification, audit |
| **W2** | W1 plus step-up re-authentication and a per-action feature flag: changes every buyer sees, price and stock, policies, receipts and shipments |
| **W3** | W2 decided by the Owner only: deletion and state changes that charge Etsy's listing fee (`financial`) |

**Rules for every write**

- It starts **owner-trigger only** on each shop: the owner runs it first, by hand.
- Its write scope is requested by re-consent at that first use.
- It is never called against a live shop from tests.
- When an employee calls it, it becomes an approval and runs outside the session.

**Phase "4 (done)"** means the read is already live in the dashboard (slice 4.6).

| # | Endpoint | Method | Scope | Dashboard feature | Tool | Granted to (draft) | Phase | Approval |
|---|---|---|---|---|---|---|---|---|
| 1 | `getBuyerTaxonomyNodes` | GET | API key | Product research: buyer categories | `etsy.taxonomy.read` | Daniel, Shon | 12 | R0 |
| 2 | `getPropertiesByBuyerTaxonomyId` | GET | API key | Product research: buyer attributes | `etsy.taxonomy.read` | Daniel, Shon | 12 | R0 |
| 3 | `getShopPaymentAccountLedgerEntries` | GET | transactions_r | Ledger, fee share trend, accountant report | `etsy.ledger.read` | Roni | 13 | R1 |
| 4 | `getShopPaymentAccountLedgerEntry` | GET | transactions_r | Ledger entry detail | `etsy.ledger.read` | Roni | 13 | R1 |
| 5 | `ping` | GET | API key | Health checks | `etsy.platform.health` | automations only | 4 (done) | R0 |
| 6 | `tokenScopes` | POST | API key | Capability probes: granted scopes | `etsy.platform.health` | automations only | 4 (done) | R0 |
| 7 | `getPaymentAccountLedgerEntryPayments` | GET | transactions_r | Payout matching | `etsy.ledger.read` | Roni | 13 | R1 |
| 8 | `getPayments` | GET | transactions_r | Finance overview, profit per order | `etsy.payments.read` | Roni | 13 | R1 |
| 9 | `getShopPaymentByReceiptId` | GET | transactions_r | Order detail: fees and net | `etsy.payments.read` | Roni | 13 | R1 |
| 10 | `getReviewsByListing` | GET | API key | Listing Health: rating per listing | `etsy.reviews.read` | Dana, Gal | 11 | R0 |
| 11 | `getReviewsByShop` | GET | API key | Customers: reviews strip; low-review alerts (Phase 6) | `etsy.reviews.read` | Dana, Gal | 4 (done) | R0 |
| 12 | `getPropertiesByTaxonomyId` | GET | API key | Listing Health: required attributes | `etsy.taxonomy.read` | Daniel, Shon | 11 | R0 |
| 13 | `getSellerTaxonomyNodes` | GET | API key | Listing Health: category checks | `etsy.taxonomy.read` | Daniel, Shon | 11 | R0 |
| 14 | `findShops` | GET | API key | Product research (Shon), never for copying | `etsy.shops.search` | Shon | 12 | R0 |
| 15 | `getShop` | GET | API key | Shop card, Statistics tab, Home | `etsy.shop.read` | Gal, Maya | 4 (done) | R0 |
| 16 | `getShopByOwnerUserId` | GET | API key | Multi-shop: the owner's other shops | `etsy.shop.read` | Gal, Maya | 21 | R0 |
| 17 | `updateShop` | PUT | shops_r, shops_w | Shop settings: announcement, vacation switch | `etsy.shop.update` | automations only | 7 | W2 |
| 18 | `getHolidayPreferences` | GET | shops_r | SLA calendar: the shop's holidays | `etsy.policies.read` | Daniel, Eitan | 6 | R0 |
| 19 | `updateHolidayPreferences` | PUT | shops_w | Holiday calendar | `etsy.holiday_preferences.update` | automations only | 6 | W1 |
| 20 | `createShopReadinessStateDefinition` | POST | shops_w | Processing profiles | `etsy.processing_profiles.manage` | automations only | 12 | W1 |
| 21 | `deleteShopReadinessStateDefinition` | DELETE | shops_w | Processing profiles | `etsy.processing_profiles.manage` | automations only | 12 | W1 |
| 22 | `getShopReadinessStateDefinition` | GET | shops_r | Processing profile detail | `etsy.policies.read` | Daniel, Eitan | 11 | R0 |
| 23 | `getShopReadinessStateDefinitions` | GET | shops_r | Shipping: processing times; ship-by SLA | `etsy.policies.read` | Daniel, Eitan | 11 | R0 |
| 24 | `updateShopReadinessStateDefinition` | PUT | shops_w | Processing profiles | `etsy.processing_profiles.manage` | automations only | 12 | W1 |
| 25 | `getShopProductionPartners` | GET | shops_r | Shipping: production partners | `etsy.production_partners.read` | Tom | 4 (done) | R0 |
| 26 | `createReceiptShipment` | POST | transactions_w | Guarded shipment write (slice 6.6) | `etsy.shipments.create` | automations only | 6 | W2 |
| 27 | `getShopReceipt` | GET | transactions_r | Receipt ingest by webhook | `etsy.receipts.read` | Dana, Eitan, Noa, Roni | 5 | R1 |
| 28 | `getShopReceipts` | GET | transactions_r | Orders, Statistics, Home; the order history store (#57) | `etsy.receipts.read` | Dana, Eitan, Noa, Roni | 4 (done) | R1 |
| 29 | `updateShopReceipt` | PUT | transactions_w | Mark shipped where tracking upload is blocked | `etsy.receipts.update` | automations only | 6 | W2 |
| 30 | `getShopReceiptTransaction` | GET | transactions_r | Transaction detail | `etsy.receipts.read` | Dana, Eitan, Noa, Roni | 5 | R1 |
| 31 | `getShopReceiptTransactionsByListing` | GET | transactions_r | Sales per listing, reconciliation | `etsy.receipts.read` | Dana, Eitan, Noa, Roni | 10 | R1 |
| 32 | `getShopReceiptTransactionsByReceipt` | GET | transactions_r | Receipt ingest | `etsy.receipts.read` | Dana, Eitan, Noa, Roni | 5 | R1 |
| 33 | `getShopReceiptTransactionsByShop` | GET | transactions_r | Not needed while receipts embed transactions | `etsy.receipts.read` | Dana, Eitan, Noa, Roni | 5 | R1 |
| 34 | `consolidateShopReturnPolicies` | POST | shops_w | Policy governance | `etsy.return_policies.manage` | automations only | 12 | W2 |
| 35 | `createShopReturnPolicy` | POST | shops_w | Policy governance | `etsy.return_policies.manage` | automations only | 12 | W2 |
| 36 | `deleteShopReturnPolicy` | DELETE | shops_w | Policy governance | `etsy.return_policies.manage` | automations only | 12 | W2 |
| 37 | `getShopReturnPolicies` | GET | API key | Shipping: return policy | `etsy.policies.read` | Daniel, Eitan | 4 (done) | R0 |
| 38 | `getShopReturnPolicy` | GET | API key | Policy governance | `etsy.policies.read` | Daniel, Eitan | 11 | R0 |
| 39 | `updateShopReturnPolicy` | PUT | shops_w | Policy governance | `etsy.return_policies.manage` | automations only | 12 | W2 |
| 40 | `createShopSection` | POST | shops_w | Products: manage sections | `etsy.sections.manage` | automations only | 7 | W1 |
| 41 | `deleteShopSection` | DELETE | shops_w | Products: manage sections | `etsy.sections.manage` | automations only | 7 | W1 |
| 42 | `getShopSection` | GET | API key | Products: section detail | `etsy.sections.read` | automations only | 11 | R0 |
| 43 | `getShopSections` | GET | API key | Products: sections filter | `etsy.sections.read` | automations only | 4 (done) | R0 |
| 44 | `updateShopSection` | PUT | shops_w | Products: manage sections | `etsy.sections.manage` | automations only | 7 | W1 |
| 45 | `createShopShippingProfile` | POST | shops_w | Shipping templates across shops | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 46 | `createShopShippingProfileDestination` | POST | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 47 | `createShopShippingProfileUpgrade` | POST | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 48 | `deleteShopShippingProfile` | DELETE | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 49 | `deleteShopShippingProfileDestination` | DELETE | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 50 | `deleteShopShippingProfileUpgrade` | DELETE | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 51 | `getShippingCarriers` | GET | API key | Tracking: carrier names | `etsy.shipping_carriers.read` | Eitan | 6 | R0 |
| 52 | `getShopShippingProfile` | GET | shops_r | Ship time per country | `etsy.shipping_profiles.read` | Daniel | 6 | R0 |
| 53 | `getShopShippingProfileDestinationsByShippingProfile` | GET | shops_r | Ship time per country | `etsy.shipping_profiles.read` | Daniel | 6 | R0 |
| 54 | `getShopShippingProfileUpgrades` | GET | shops_r | Shipping upgrades | `etsy.shipping_profiles.read` | Daniel | 6 | R0 |
| 55 | `getShopShippingProfiles` | GET | shops_r | Shipping: profiles | `etsy.shipping_profiles.read` | Daniel | 4 (done) | R0 |
| 56 | `updateShopShippingProfile` | PUT | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 57 | `updateShopShippingProfileDestination` | PUT | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 58 | `updateShopShippingProfileUpgrade` | PUT | shops_w | Shipping templates | `etsy.shipping_profiles.manage` | automations only | 12 | W2 |
| 59 | `createDraftListing` | POST | listings_w | Product Studio: draft | `etsy.listings.create_draft` | automations only | 12 | W1 |
| 60 | `deleteListing` | DELETE | listings_d | Catalog clean-up (Owner only) | `etsy.listings.delete` | automations only | 12 | W3 |
| 61 | `deleteListingProperty` | DELETE | listings_w | Listing Health: attribute fixes | `etsy.listing_properties.manage` | Daniel | 11 | W1 |
| 62 | `findAllActiveListingsByShop` | GET | API key | Product research (Shon) | `etsy.listings.search_public` | Shon | 12 | R0 |
| 63 | `findAllListingsActive` | GET | API key | Product research (Shon) | `etsy.listings.search_public` | Shon | 12 | R0 |
| 64 | `getFeaturedListingsByShop` | GET | API key | Listing Health: featured listings (read-only since 2026-10-14) | `etsy.shop.read` | Gal, Maya | 11 | R0 |
| 65 | `getListing` | GET | API key | Listing detail, write verification (re-read) | `etsy.listings.read` | Daniel, Gal, Noa, Noam | 11 | R0 |
| 66 | `getListingProperties` | GET | API key | Listing Health: attribute completeness | `etsy.listing_details.read` | Daniel, Noam | 11 | R0 |
| 67 | `getListingProperty` | GET | API key | Listing Health: attribute completeness | `etsy.listing_details.read` | Daniel, Noam | 11 | R0 |
| 68 | `getListingsByListingIds` | GET | API key | Listing Health batch sync | `etsy.listings.read` | Daniel, Gal, Noa, Noam | 11 | R0 |
| 69 | `getListingsByShop` | GET | listings_r | Products catalog, Listing Health sync | `etsy.listings.read` | Daniel, Gal, Noa, Noam | 4 (done) | R0 |
| 70 | `getListingsByShopReceipt` | GET | transactions_r | Order detail: listings of a receipt | `etsy.listings.read` | Daniel, Gal, Noa, Noam | 5 | R0 |
| 71 | `getListingsByShopReturnPolicy` | GET | listings_r | Policy governance: listings per return policy | `etsy.listings.read` | Daniel, Gal, Noa, Noam | 11 | R0 |
| 72 | `getListingsByShopSectionId` | GET | API key | Products: listings per section | `etsy.sections.read` | automations only | 11 | R0 |
| 73 | `getListingsShippingByListingIds` | GET | shops_r | Listing Health: shipping completeness | `etsy.listing_shipping.read` | Daniel | 11 | R0 |
| 74 | `updateListing` | PATCH | listings_w | Listing Health fixes; activation, deactivation and renewal (listing fee) in Product Studio | `etsy.listings.update` + `etsy.listings.change_state` | Daniel | 11 (content), 12 (state) | W2 / W3 |
| 75 | `updateListingProperty` | PUT | listings_w | Listing Health: attribute fixes | `etsy.listing_properties.manage` | Daniel | 11 | W1 |
| 76 | `deleteListingFile` | DELETE | listings_w | Product Studio: digital files | `etsy.listing_files.manage` | automations only | 12 | W1 |
| 77 | `getAllListingFiles` | GET | listings_r | Digital products | `etsy.listing_media.read` | Nika | 12 | R0 |
| 78 | `getListingFile` | GET | listings_r | Digital products | `etsy.listing_media.read` | Nika | 12 | R0 |
| 79 | `uploadListingFile` | POST | listings_w | Product Studio: digital files | `etsy.listing_files.manage` | automations only | 12 | W1 |
| 80 | `deleteListingImage` | DELETE | listings_w | Product Studio: media | `etsy.listing_media.manage` | Nika | 12 | W1 |
| 81 | `getListingImage` | GET | API key | Listing Health: image checks | `etsy.listing_media.read` | Nika | 11 | R0 |
| 82 | `getListingImages` | GET | API key | Products: images | `etsy.listing_media.read` | Nika | 4 (done) | R0 |
| 83 | `uploadListingImage` | POST | listings_w | Product Studio: media | `etsy.listing_media.manage` | Nika | 12 | W1 |
| 84 | `getListingInventory` | GET | listings_r | Products: stock per variation | `etsy.inventory.read` | Daniel, Tom | 11 | R0 |
| 85 | `getListingsInventoryByListingIds` | GET | listings_r | Nightly inventory reconciliation | `etsy.inventory.read` | Daniel, Tom | 11 | R0 |
| 86 | `updateListingInventory` | PUT | listings_w | Price and stock sync from supplier data | `etsy.inventory.update` | automations only | 13 | W2 |
| 87 | `getListingOffering` | GET | API key | Products: offering detail | `etsy.inventory.read` | Daniel, Tom | 11 | R0 |
| 88 | `deleteListingPersonalization` | DELETE | listings_w | Product Studio: personalization | `etsy.listing_personalization.manage` | automations only | 12 | W1 |
| 89 | `getListingPersonalization` | GET | API key | Listing Health: personalization | `etsy.listing_details.read` | Daniel, Noam | 11 | R0 |
| 90 | `updateListingPersonalization` | POST | listings_w | Product Studio: personalization | `etsy.listing_personalization.manage` | automations only | 12 | W1 |
| 91 | `getListingProduct` | GET | listings_r | Products: variation detail | `etsy.inventory.read` | Daniel, Tom | 11 | R0 |
| 92 | `createListingTranslation` | POST | listings_w | Product Studio: translations | `etsy.listing_translations.manage` | Noam | 12 | W1 |
| 93 | `getListingTranslation` | GET | API key | Listing Health: translations | `etsy.listing_details.read` | Daniel, Noam | 11 | R0 |
| 94 | `updateListingTranslation` | PUT | listings_w | Listing Health: translation fixes | `etsy.listing_translations.manage` | Noam | 11 | W1 |
| 95 | `getListingVariationImages` | GET | API key | Listing Health: variation images | `etsy.listing_media.read` | Nika | 11 | R0 |
| 96 | `updateVariationImages` | POST | listings_w | Product Studio: variation images | `etsy.listing_media.manage` | Nika | 12 | W1 |
| 97 | `deleteListingVideo` | DELETE | listings_w | Product Studio: media | `etsy.listing_media.manage` | Nika | 12 | W1 |
| 98 | `getListingVideo` | GET | API key | Listing Health: video presence | `etsy.listing_media.read` | Nika | 11 | R0 |
| 99 | `getListingVideos` | GET | API key | Listing Health: video presence | `etsy.listing_media.read` | Nika | 11 | R0 |
| 100 | `uploadListingVideo` | POST | listings_w | Product Studio: media | `etsy.listing_media.manage` | Nika | 12 | W1 |
| 101 | `getMe` | GET | shops_r | Connect flow: the connected user | `etsy.account.read` | automations only | 4 (done) | R1 |
| 102 | `getUser` | GET | email_r | Needs email_r; not requested (no use) | `etsy.account.read` | automations only | not planned | R1 |
| 103 | `getUserAddress` | GET | address_r | Needs address_r; not requested (no use) | `etsy.account.read` | automations only | not planned | R1 |
| 104 | `getUserAddresses` | GET | address_r | Needs address_r; not requested (no use) | `etsy.account.read` | automations only | not planned | R1 |

### Webhook events

All 11 events are routed in [Phase 5 slice 5.3](../plan/phases/phase-05-orders-ingest.md#53--webhook-ingress-for-all-11-etsy-events): `order.paid`, `order.canceled`, `order.shipped`, `order.delivered`, `listing.created`, `listing.updated`, `listing.deleted`, `inventory.created`, `inventory.updated`, `inventory.deleted` and `inventory.out_of_stock`.

### What the API cannot do, and what HTCO does instead

| Gap | Alternative (first choice first) | Phase |
|---|---|---|
| Shop stats: visits, views, conversion, search terms | The owner uploads Shop Manager's CSV export (no automation needed); the stats export recipe B-02 where scoped | 11 (upload), 8A (B-02) |
| Etsy Ads | CSV upload of the Ads report; recipe B-03 where scoped; budget changes as a Human task, or B-10 | 11, 8A, 13 |
| Buyer messages and cases | Etsy's notification emails read from the owner's mailbox (Gmail read-only); replies through delivery B, or B-05 where scoped | 14 |
| Refunds, cancellations | A Human task with the approved values; B-09 only with written confirmation | 13 |
| Coupons and sales | A Human task with the approved terms; B-08 only with written confirmation | 13 |
| Review replies | A drafted reply plus a Human task; B-07 where scoped | 14 |
| Tracking upload where `createReceiptShipment` is blocked for the key | POD or fulfillment partner integrations (Printify, Printful, AutoVA); a partner's site through a scoped recipe; a Human task | 6, 13 |
| Monthly statements | Ledger entries (API) give the same lines; the statement download (B-11) where scoped | 13 |
| Shipping labels | Not offered by Etsy's API; not automated | — |

## Rules we keep (API Terms, 2026-08-18)

- One app (one keystring) for all shops. Never extra keys or apps to get around limits or review.
- Minimum data, minimal caching, and no data shown older than 6 hours (listings) or 24 hours (other data).
- Report a breach within 24 hours.
- Show the trademark notice: "The term 'Etsy' is a trademark of Etsy, Inc." and that HTCO is not endorsed by Etsy.
- No scraping, and no messages to buyers through the API about orders or shipping. Browser automation on etsy.com runs only inside the scope that the owner records from Etsy's approval ([etsy-automation-approval.md](etsy-automation-approval.md)), each action behind its own flag (ADR-0022).
- No training AI on Etsy content. AI drafts are approved by a human before they reach Etsy.

## Open questions (test with our own key in Phase 4)

- Do `createReceiptShipment` and the receipt addresses work for an Israeli shop on our key?
- Which tier do we get, and what are its rate limits and webhook access?
- Does `updateShopReceipt` (`was_shipped`) still work where tracking is blocked?
