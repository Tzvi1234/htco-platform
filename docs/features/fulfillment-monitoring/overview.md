# Fulfillment monitoring — Overview

| | |
|---|---|
| **Slug** | `fulfillment-monitoring` |
| **Phase / slices** | Phase 6 — 6.1 (tracking detection) and 6.2 (SLA policies and the inspector) built; 6.9 (ship-by, low-review and stock alerts) and 6.10 (the AutoVA tracking relay) follow |
| **Requirements** | [FR-ORD-06, FR-INC-01](../../product/PRD.md), FR-AUT-05 |
| **Decisions** | [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (engine and gateway), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools) |
| **Status** | Built, inactive: both automations are `draft` until the owner activates them (slice 6.7); the 15-minute schedule is off unless `HTCO_WORKER_SCHEDULES` names `fulfillment.inspect` |
| **Last updated** | 2026-10-25 |

## What and why

HTCO notices fulfillment problems before buyers do. Two deterministic automations, no AI:

- **Tracking Synchronization** (`tracking-sync`) reads each shipped receipt through the Action Gateway (`etsy.receipts.read`, GET only) and records every shipment once in `tracking_events`, with when it first appeared and from where. It never writes to Etsy and never moves the order: the order's move to `SHIPPED` belongs to Etsy Order Intake's state machine (slice 5.4).
- **Incident Detection & Escalation** (`incident-detection-escalation`) checks every paid, unshipped order without tracking against its SLA policy. An order past its window opens exactly one incident (see [incidents](../incidents/overview.md)); tracking closes it on that evidence.

The default window is **seven business days** from payment to the first tracking (the owner's decision of 2026-10-23, replacing the first ten). A policy row per shop, supplier or shipping method overrides it once the owner agrees those windows from data.

## Users and actors

- **Etsy**: the `order.shipped` webhook (slice 5.3) starts Tracking Synchronization for the receipt.
- **The scheduler** (`fulfillment.inspect`, every 15 minutes, per connected shop) starts both automations.
- **The owner** agrees the SLA windows and activates the automations (6.7).

## Acceptance criteria

| ID | Criterion |
|---|---|
| AC-6.1.1 | A receipt with multiple shipments records each once; repeated observation adds no duplicates. |
| AC-6.1.2 | The order timeline shows when tracking first appeared and from which source. |
| AC-6.2.1 | An order inside its window never produces a breach (boundary tests at exactly the deadline). |
| AC-6.2.2 | Weekends and configured holidays are excluded when the policy says business days. |

## Out of scope here

The relay's upload into Etsy (the guarded `etsy.shipments.create`, 6.8) and its screen (6.10 PR B), writes to Etsy (6.6, 6.8), the timeline view on the Orders screen (needs 5.7).

## The AutoVA relay (slice 6.10, the Human-task path)

The owner orders through AutoVA and copies each tracking number into Etsy by hand (A-05). The relay keeps that work in the office until Phase 8A reads AutoVA itself:

- **Detect (AC-6.10.1).** Each inspection (`fulfillment.inspect`) opens one relay item per paid order without tracking strictly past the relay window: 2 business days after payment (A-20). One item per order, ever (`app.relay_items`, `shop_id, receipt_id` unique); the Owners get one `relay_tracking_needed` notification when it opens.
- **Paste (AC-6.10.4).** The paste (the parcels route in the relay-parcels PR, option (a); the single-code route is gone) takes the carrier and the number as AutoVA shows it. `TrackingNumber.parse` runs before anything else; an unknown carrier or a number in the wrong format is a 422 `tracking-invalid` whose message names the carrier, never the number. Owners and Admins, or Operations and Support on their granted shops (update incidents and buyer data).
- **Never twice (AC-6.10.2).** A number Tracking Synchronization already saw on the receipt closes the item on that evidence and is not kept; a number pasted for another order of the shop is a 409 `tracking-already-used`; a second paste is a 409 `relay-item-moved`.
- **Hand off (AC-6.10.3).** Otherwise the item moves to `handed_off`: a person copies the number into Etsy from the parcels code route (buyer-data roles only, audited, `no-store`). Never silently: the item stays open until the tracking shows on the receipt. No Etsy write here; the guarded upload is 6.8.
- **Close on evidence.** When Tracking Synchronization records the order's tracking from Etsy, the item closes (`tracking_on_etsy`) and its code is deleted. Each inspection also closes the items of orders cancelled or refunded on Etsy (`cancelled`) and those handed off more than 30 days ago (`expired`), and the disconnect purge removes a shop's items with their codes. The relay's notification is asked for again while the item waits, so one that failed is sent later (deduplicated), and the window counts the shop's own business days (its SLA policy's calendar).
- **Activation order.** Tracking Synchronization is activated before (or with) Incident Detection: without it, tracking that reached Etsy would not close the items (slice 6.7).

Tests: `tests/application/test_autova_relay.py` (every AC), `tests/api/test_relay_routes.py`, `tests/integration/test_relay_store.py` (db), `supabase/tests/database/27_relay_items.test.sql`.

## Alerts (slice 6.9)

- **Ship-by:** each inspection warns once per order (`alert_ship_by`, in-app and email) from one business day before the earliest item's `expected_ship_date`, counting the shop's business days. The calendar, holidays included, is the shop's SLA policy calendar until the shop's Etsy holiday preferences are read (`etsy.holiday_preferences`, a later step). AC-6.9.1: `test_ac_6_9_1_inside_the_warning_window_one_alert_once`, `test_ac_6_9_1_before_the_window_no_alert_and_holidays_are_skipped`.
- **Stock:** `catalog.out_of_stock.v1`, which the webhook job publishes only when the re-read confirms it, raises one in-app alert per listing and Etsy change (`StockAlerts`).
- **Low reviews (AC-6.9.2):** every hour per shop (`fulfillment.reviews`, off unless `HTCO_WORKER_SCHEDULES` names it), Incident Detection & Escalation runs in its `reviews` mode. It reads the reviews written in the last 48 hours through the Action Gateway (`etsy.reviews.read`, R0, getReviewsByShop, GET only), and raises one alert (`alert_low_review`) per review of **3 stars or fewer** (the owner's decision of 2026-10-23). The alert names the stars and the listing and, when the order is in the order history, the order (found by the review's `transaction_id` in `etsy_order_items`). The key is the shop and the transaction, so a review read again the next hour alerts once. The review's text, the buyer's words, never leaves the gateway's API channel. Tests: `test_ac_6_9_2_a_review_of_3_stars_or_fewer_alerts_once_with_listing_and_order`, `test_ac_6_9_2_the_read_goes_through_the_gateway_as_the_automation`, `test_worker__a_reviews_transaction_finds_its_order_in_its_own_shop_only`.

Alerts are notifications. There is one path, the one in 6.4, and no alert table.
