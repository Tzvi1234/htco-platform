# Fulfillment monitoring — Security

| Threat | Control |
|---|---|
| A write to the live shop (LeatherPY) | Tracking Synchronization's only Etsy action is `etsy.receipts.read` (R1, GET) through the gateway; `allowed_actions` in its registry entry names nothing else; the adapter refuses any non-GET (ADR-0008). Incident Detection's only Etsy action is `etsy.reviews.read` (R0, GET, slice 6.9), the one entry in its `allowed_actions`. The reviews' text is left out of the action's output (`test_api__reviews__identifiers_and_stars_never_the_buyers_words`) |
| A pasted relay code (slice 6.10) | Kept only in `app.relay_item_codes` while the item waits for its upload, read only by the shop's buyer-data roles (RLS on `user_buyer_data_shop_ids`, and `relay_code` in the API), served `no-store`, deleted when the item closes; never in a list, an audit (`relay.tracking_pasted` keeps the carrier and last four), an event or a log. pgTAP 27, `test_the_code_is_given_back_for_the_copy_to_buyer_data_roles_only` |
| Tracking codes locate the buyer's parcel | Codes are never stored outside the buyer table (and the relay's code table above): `tracking_events` keeps a SHA-256 fingerprint (salted with the shop ID) and the last four characters; `TrackingNumber` and `OrderShipment` hide the code in their repr; events carry the carrier and IDs only; validation errors never quote the code |
| Cross-tenant reads or writes | RLS on every new table: members read their organization's rows of their shops; the worker reads and writes only its job's organization (pgTAP) |
| Worker over-privilege | The worker gains `select` on `etsy_orders.etsy_created_at` only; it still reads no money and no buyer column (pgTAP) |
| Alert floods | One incident per order and cause (FR-INC-03); generous default windows; the schedule is off by flag |

ASVS: V8.3 (sensitive data minimised), V4.1 (access control enforced server side), V7.1 (no sensitive data in logs).
