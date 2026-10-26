# Fulfillment monitoring — Design

## Layers

| Layer | Module | What it holds |
|---|---|---|
| Domain | `htco_domain.fulfillment` | Carriers and tracking formats (`TrackingNumber.parse`), the code's fingerprint, `BusinessCalendar`, `SlaPolicy` with `deadline` and `is_breached`, `select_policy`, `ship_by_at_risk` |
| Application | `htco_application.tracking_sync` | `TrackingRecorder` (shared with the relay of 6.10), the `tracking-sync` handler, `EngineTrackingSync` (the webhook trigger) |
| Application | `htco_application.sla_inspector` | The `incident-detection-escalation` handler: inspect, follow up, drifts |
| Application | `htco_application.fulfillment_schedule` | The 15-minute schedule, its per-shop job |
| Persistence | `htco_persistence.fulfillment` | `PostgresTrackingStore`, `PostgresSlaStore` (and the incident store) |
| Contracts | `packages/contracts/automations/` | Input and output schemas of both automations |

## Data

Migration `20261025100000_fulfillment_incidents.sql`:

- `app.tracking_events`: `(shop_id, receipt_id, fingerprint)` unique. The fingerprint is SHA-256 of `htco-tracking:v1:{shop_id}:{normalised code}`; the row keeps the carrier (ours and Etsy's name), the last four characters, `source` (`etsy`, `autova_manual`, `autova_browser`, `autova_email`), `observed_via` (`webhook`, `schedule`, `relay`), `first_observed_at` and Etsy's `notified_at`. The full code is not here: it is buyer data, kept by the order history in `etsy_order_buyers.shipments` (ADR-0018).
- `app.tracking_checks`: the schedule's cursor, one row per receipt with the Etsy version last read. A receipt is due again only when Etsy changes it, so receipts without a code never pin the batch and a second parcel is read (Codex review of #79).
- `app.sla_policies`: one per organization, shop, supplier and shipping method (`unique nulls not distinct`), `window_days` 1–90, `unit` (`calendar_days`, `business_days`), the calendar's time zone, weekend days and holidays. No row: the built-in default (7 business days, Saturday and Sunday off, UTC; the owner's decision of 2026-10-23, replacing the first 10, which the migration's comment still names). Supplier and shipping-method scopes are refused by a check constraint until orders carry those values (Phase 13); the domain already selects by them.

## Flows

**Webhook.** `order.shipped` → the webhook job starts Etsy Order Intake *and* Tracking Synchronization (`tracking:{shop}:{receipt}:order.shipped:{webhook id}`: one run per delivery) → the handler reads the receipt through the gateway → each shipment with a code → `record_tracking` (insert or the existing row) → on a new row, `fulfillment.tracking_detected.v1` in the same transaction → the order's `tracking_missing` incident resolves on that evidence.

**Schedule.** `fulfillment.inspect` (every 15 minutes) → one `fulfillment.inspect_shop` job per connected shop and period → it triggers `tracking-sync` (`mode = schedule`: up to 25 stored orders Etsy marks shipped without a tracking event) and `incident-detection-escalation` (`mode = inspect`), each keyed `{automation}:{shop}:{period}`.

**Inspect.** Policies of the organization; orders paid, not shipped, in `ingested` or `awaiting_shipment`, without a tracking event; the most specific policy; breached strictly after the deadline → one incident and one `fulfillment.sla_breached.v1` (key per order, policy and version). Then the follow-up: incidents of orders now cancelled or refunded resolve (`order_closed`); incidents older than their severity allows escalate once.

## Decisions

- **Order Intake owns the order state.** Tracking Synchronization records tracking and proves it; it never writes `order_status_events`, so the schedule and the webhook together give one transition to `SHIPPED` (AC-6.7.2).
- **Stores are written directly, like Order Intake.** The business reads go through the gateway (API channel); the automation's own tables are written by its store in one transaction with its events, as Order Intake writes the order history (slice 5.4). Internal-channel actions for these writes are not needed until an employee may call them (Phase 9).
- **Paid time.** The window counts from `etsy_orders.etsy_created_at`: Etsy's receipt creation, which for paid orders is the payment moment within minutes. The worker gained `select` on that one column.
- **The owner's default.** Seven business days (decision of 2026-10-23; it started at a generous ten). Per-shop, supplier and shipping-method windows come as policy rows once the owner agrees them from the Phase 0 data.
