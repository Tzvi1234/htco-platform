-- The worker's purge deletes a disconnected shop's stored order history (post-merge review of
-- 2026-10-23, P1).
--
-- The daily purge job (`shops.purge`, slice 5.1) deleted the probe results only and then cleared
-- `purge_after`; `private.purge_disconnected_etsy_orders` needs `purge_after <= now`, so it could
-- never match the shop again and its buyer names and addresses would have stayed for ever. The
-- purge now deletes the shop's orders (items, refunds and buyer rows cascade) and its sync state
-- in the same transaction, as `htco_worker` inside the job's organization.
--
-- Least privilege: the worker may delete those two tables' rows of its job's organization and
-- read only their tenant keys (a delete's WHERE reads them). It is granted nothing on the item,
-- refund and buyer tables: the cascade runs as the tables' owner. No other role changes.
--
-- Safe on the live database: grants and policies only (no table rewrite, no data change); each
-- statement takes a brief lock on its table.

grant select (organization_id, shop_id), delete on app.etsy_orders to htco_worker;
create policy etsy_orders_worker_select on app.etsy_orders for select to htco_worker
  using (organization_id = (select private.worker_organization_id()));
create policy etsy_orders_worker_delete on app.etsy_orders for delete to htco_worker
  using (organization_id = (select private.worker_organization_id()));

grant select (organization_id, shop_id), delete on app.etsy_order_sync_state to htco_worker;
create policy etsy_order_sync_state_worker_select on app.etsy_order_sync_state for select
  to htco_worker using (organization_id = (select private.worker_organization_id()));
create policy etsy_order_sync_state_worker_delete on app.etsy_order_sync_state for delete
  to htco_worker using (organization_id = (select private.worker_organization_id()));
