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

Windows start **generous**: ten business days from payment to the first tracking, until the owner agrees the real windows from data.

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

The relay that copies tracking from AutoVA into Etsy (6.10), writes to Etsy (6.6, 6.8), the timeline view on the Orders screen (needs 5.7).
