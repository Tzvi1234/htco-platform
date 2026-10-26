# Feature Backlog

| | |
|---|---|
| **Status** | 1.0 (2026-10-23). Each feature is placed in a phase of the [plan](../plan/PLAN.md) and moves into that phase file's slices at the phase's kickoff |
| **Related** | [Etsy API capabilities](../architecture/etsy-api-capabilities.md) (the endpoint inventory and the gaps), [Etsy automation approval](../architecture/etsy-automation-approval.md) (browser scope), [execution engine](../architecture/execution-engine.md) |

**Kind**

- **D (deterministic automation):** code, rules and the engine. No model calls. It can ship as soon as its data and phase exist, often before any AI employee is active.
- **AI (AI-agent-dependent):** needs an active employee (Phase 9 onwards), its evaluation and its routing budget. It ships later.
- **D+AI:** the deterministic part ships first; an employee adds drafts or explanations later.

**Channel:** API (Etsy Open API), Upload (the owner uploads an export), Browser (a scoped recipe, flag off until its scope row is recorded), Human (a task for a person), Email (Etsy's notification emails).

## 1. When the API cannot do it

| Feature | How | Channel | Kind | Phase |
|---|---|---|---|---|
| Shop stats: visits, views, conversion, traffic sources, search terms | Upload of Shop Manager's stats CSV, parsed and validated into `shop_traffic_daily` and `listing_metrics`; recipe B-02 where scoped | Upload, then Browser | D | 11 (upload), 8A (B-02) |
| Etsy Ads results and spend | Upload of the Ads report CSV into `ads_daily`; recipe B-03 where scoped | Upload, then Browser | D | 11, 8A |
| Buyer messages | Etsy notification emails (Gmail, read-only) matched to orders; replies drafted by Dana; delivery B (the owner's browser) or B-05 where scoped | Email, Human, Browser | D+AI | 14 |
| Cases and help requests | Notification emails; a case task with the order's evidence; B-06 to read where scoped | Email, Human | D | 14 |
| Refunds and cancellations | A Human task with the approved amount and reason; B-09 only with the written confirmation | Human | D | 13 |
| Coupons and sales | A Human task with the approved terms; B-08 only with the written confirmation | Human | D | 13 |
| Review replies | A drafted reply (Dana) plus a Human task to post it; B-07 where scoped | Human, Browser | D+AI | 14 |
| Tracking where `createReceiptShipment` is blocked | POD or fulfillment partner tracking (Printify, Printful, AutoVA), a partner's site through a scoped recipe, or a Human task | API (partner), Browser, Human | D | 6, 13 |
| Monthly statement | Ledger entries through the API; statement download (B-11) where scoped | API, Browser | D | 13 |

## 2. Owner's new ideas

| Feature | What it shows or does | Data | Kind | Phase |
|---|---|---|---|---|
| **Profit per order** | AutoVA's profit field shown at once (estimated); then revenue − Etsy fees (ledger) − supplier cost − shipping − ads share, cross-checked with AutoVA's figure, a difference opens a finding | `autova_orders` (A-01), `etsy.ledger.read`, `etsy.payments.read`, supplier mappings | D | 8A (AutoVA figure), 13 (verified) |
| **Ship-by SLA alerts** | Warning before `expected_ship_date` without a shipment; overdue escalation | Receipts, processing profiles, holidays | D | 6 (slice 6.9) |
| **Refund-reason analysis → improvement tasks** | Groups refunds and cancellations by Etsy's reason and Eitan's annotated reason; opens one improvement task per recurring cause (listing, supplier, packaging) | `etsy_order_refunds` (#57), `orders.annotate` | D+AI | 9 (Eitan's reasons), 10 (tasks from patterns) |
| **Seasonality and stock forecasts** | Units per listing by week and month, last year vs this year, a simple forecast (seasonal naive, then exponential smoothing) with a stock-out date | Order history store, inventory | D | 13 |
| **Holiday calendar** | Etsy's holiday preferences per country, our business-day calendar, marketplace peaks (Black Friday, Mother's Day, Hanukkah, Christmas) with lead times | `etsy.policies.read`, `etsy.holiday_preferences.update` | D | 6 |
| **Buyer-country map with ship time per country** | Orders by destination country and the median days from paid to shipped and to delivered | `country_iso`, shipments, `order.delivered` | D | 6 |
| **Repeat customers** | Share of orders from returning buyers, computed on demand from the order history (no stored customer list, ADR-0018) | Order history store | D | 10 |
| **Fee share trend** | Etsy fees as a share of revenue per month, split by fee type (transaction, processing, listing, ads, offsite ads) | Ledger | D | 13 |
| **Listing health checks** | Tags, title, photos and video, attributes, processing profile, ECGT fields, expiring listings | Listing tools | D+AI | 11 (deterministic checks first, Daniel and Noam after) |
| **Morning summary on WhatsApp or Telegram** | Yesterday's orders, revenue, late shipments, incidents, approvals waiting, low reviews; sent at a set time | Scheduled Reporting (7.9) | D | 7 (email and in-app); the WhatsApp or Telegram channel through Hermes (D-10) in 15 |
| **Accountant report** | Monthly CSV and PDF: revenue, fees, refunds, payouts, VAT-relevant totals per country, reconciled with the ledger | Ledger, payments, Order & Financial Reconciliation | D | 13 |
| **Multi-shop comparison** | Shops side by side: revenue, orders, average order value, profit, ship time, review average, fee share | Per-shop stores | D | 21 (from 2 shops; the view can ship in 13) |
| **Sale before/after results** | For a sale or coupon (recorded as a Human task or B-08), orders, revenue and margin for the same days before vs during vs after | Order history, the task record | D | 13 |
| **Low-review alerts with drafted replies** | Alert for ≤ 3 stars (6.9); a drafted reply and a recovery task (Dana) | `etsy.reviews.read` | D+AI | 6 (alert), 14 (draft) |
| **AutoVA quotes for a new product** | Every quote email parsed at once (no model) and shown under its product on a "New product quotes" screen, with a notification; the owner picks one (approval every time); then the quote is approved in AutoVA (Human task, then recipe A-02) and the supplier mapping is recorded | Quote mailbox (D-23), `supplier_quotes` | D | 7 (7.13–7.17), 8A (A-02) |
| **AutoVA order monitor** | Every AutoVA order's status, paid flag, carrier and tracking from the Export (screen read as fallback); an Unpaid order past its window alerts the owner (payment stays AutoVA's Auto Pay; HTCO never pays) | Recipe A-01 | D | 8A |
| **AutoVA tracking to Etsy** | The relay copies AutoVA's carrier and tracking number to Etsy (API first; B-12 or a Human task) | A-01, `etsy.shipments.create` | D | 6 (6.10, Human task until 8A), 8A (A-01) |
| **Model mode per employee** | The Owner chooses, per employee, "routed" (the model follows the task's difficulty, up to the employee's cap) or "pinned" (one fixed model by catalog alias, for example `default-advanced`, with an explicit fallback, or a stop). Each change is audited and makes a new profile version ([ADR-0023](../adr/0023-routing-modes-and-difficulty-assessment.md)) | `model_catalog_aliases`, `employee_routing_settings`, routing decisions | D | 9 (9.2, 9.3, 9.8) |
| **Automatic task difficulty** | A task that declares no difficulty is judged by rules, then by Jev "Ultrafast" (simple, normal, complex) once its benchmark passes. It can only raise the model tier within the employee's cap, so cheap tasks stay cheap | Task features (no buyer PII), decision records | D+AI | 9 (9.3, 9.6, 9.9) |
| **Shop knowledge base** | Any employee can search the shop's own policies, FAQ, listings, products and past approved replies (redacted). Structured tools answer first; then one hybrid full-text and vector search, filtered by organization and shop; then the model. Never a raw buyer message ([ADR-0024](../adr/0024-knowledge-base-and-retrieval.md)) | pgvector in Supabase, `knowledge_sources`, `knowledge_chunks` | D+AI | 9 (9.10), 14 (Dana, first consumer) |

## 3. More from the Etsy docs

| Feature | What | Data | Kind | Phase |
|---|---|---|---|---|
| Vacation mode switch | Turn vacation mode on and off with a message, from HTCO | `etsy.shop.update` (W2, owner-trigger first) | D | 7 (7.12) |
| Expiring listings | Listings whose `ending_timestamp` is within 14 days; renewal as an approval (fee) | Listings, `etsy.listings.change_state` (W3) | D | 11 |
| Out-of-stock reaction | `inventory.out_of_stock` webhook → alert, and for supplier-mapped variants a restock or deactivate proposal | Webhook, inventory | D | 6 (alert), 13 (proposal) |
| Unintended listing edits | `listing.updated` webhook diffed against the last approved state; an alert for changes HTCO did not make | Webhook, listing snapshots | D | 11 |
| Attribute completeness | Required properties per taxonomy node vs the listing's properties | `etsy.taxonomy.read`, `etsy.listing_details.read` | D | 11 |
| Shipping profile consistency | Listings without a profile, destinations without upgrades, profiles with out-of-date processing times | Shipping profiles, listings | D | 11 |
| Return policy governance | Listings per return policy, and consolidation of duplicates (approval) | `getListingsByShopReturnPolicy`, `consolidateShopReturnPolicies` | D | 12 |
| Translation coverage | Languages present per listing; Noam drafts missing translations for approval | Translations | D+AI | 11 (coverage), 12 (drafts) |
| Personalization review | Listings with personalization questions vs their orders' personalization errors | Personalization, order notes (no buyer contact data) | D | 11 |
| Production partner disclosure | Listings whose production partner is missing or inconsistent with the catalog's supplier | Production partners, catalog | D | 12 |
| Digital file checks | Digital listings without files, or with outdated files | Listing files | D | 12 |
| Payout matching | Ledger disbursements matched to bank deposits (owner's CSV) | Ledger | D | 13 |
| Featured listings check | Featured listings that are inactive, out of stock or unhealthy | Featured listings (read-only) | D | 11 |
| Token and scope health | Days until the refresh token's 90-day expiry; scopes missing for enabled write tools | `tokenScopes`, vault | D | 5 (maintenance, #53) |

## 4. Placement summary

| Phase | Deterministic features | AI-dependent features |
|---|---|---|
| 5 | Order store on the queue, webhooks for all 11 events, token and scope health | — |
| 6 | Ship-by SLA, holiday calendar, country map and ship times, low-review and out-of-stock alerts, owner-triggered shipment writes | — |
| 7 | Morning summary (email and in-app), vacation switch, sections, holiday writes, AutoVA quotes from email to the owner's choice | — |
| 8A | AutoVA recipes A-01 (orders, tracking, profit; Export first) and A-02 (approve the chosen quote); stats and ads exports (B-02, B-03) where scoped | — |
| 9 | Model mode per employee (routed or pinned); the shop knowledge base's store, ingestion and hybrid search | Eitan's refund and cancellation reasons; automatic task difficulty (Jev, after its benchmark) |
| 10 | Repeat customers, refund-pattern improvement tasks | Noa's review of findings |
| 11 | Health checks, expiring listings, unintended edits, attribute, shipping and translation coverage, stats and ads uploads | Daniel's audits, Noam's fixes |
| 12 | Return policy governance, production partner and digital file checks | Shon's research, Noam's translations, Nika's media |
| 13 | Profit per order, seasonality and stock forecasts, fee share trend, accountant report, sale before/after, payout matching, money actions as Human tasks | Roni's narrative, Tom's restock proposals |
| 14 | Message and case intake from emails | Dana's drafted replies (messages and reviews) |
| 15 | Morning summary on WhatsApp or Telegram (through Hermes, D-10) | Lia presents the summary |
| 21 | Multi-shop comparison | Gal's experiments |
