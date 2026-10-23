# Fulfillment monitoring — Security

| Threat | Control |
|---|---|
| A write to the live shop (LeatherPY) | Tracking Synchronization's only Etsy action is `etsy.receipts.read` (R1, GET) through the gateway; `allowed_actions` in its registry entry names nothing else; the adapter refuses any non-GET (ADR-0008). Incident Detection makes no Etsy call |
| Tracking codes locate the buyer's parcel | Codes are never stored outside the buyer table: `tracking_events` keeps a SHA-256 fingerprint (salted with the shop ID) and the last four characters; `TrackingNumber` and `OrderShipment` hide the code in their repr; events carry the carrier and IDs only; validation errors never quote the code |
| Cross-tenant reads or writes | RLS on every new table: members read their organization's rows of their shops; the worker reads and writes only its job's organization (pgTAP) |
| Worker over-privilege | The worker gains `select` on `etsy_orders.etsy_created_at` only; it still reads no money and no buyer column (pgTAP) |
| Alert floods | One incident per order and cause (FR-INC-03); generous default windows; the schedule is off by flag |

ASVS: V8.3 (sensitive data minimised), V4.1 (access control enforced server side), V7.1 (no sensitive data in logs).
