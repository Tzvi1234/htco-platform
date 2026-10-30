-- The disconnect purge deletes the shop's product sales counters with its order history (Codex
-- post-merge review, owner gate v4, P1).
--
-- Deleting `app.etsy_orders` cascades into `app.product_sales_applications`, the ledger of what
-- each receipt contributed, but `app.product_sales_counters` stayed. On a reconnect the backfill
-- restored the receipts, the rebuild saw no applications and added every unit again (ten counted
-- units became twenty). The purge now deletes the counters in the same transaction, so the
-- counters and their ledger are always purged together.
--
-- Least privilege: the worker gains `delete` on the counters only; the existing
-- `product_sales_counters_worker` policy (for all, its job's organization) already scopes it.
--
-- Safe on the live database: one grant (no table rewrite, no data change).

grant delete on app.product_sales_counters to htco_worker;
