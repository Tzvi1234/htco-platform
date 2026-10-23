-- Ship-by alerts (slice 6.9): the worker reads each item's ship-by date (`expected_ship_date`,
-- Etsy's own), to warn before an unshipped order is late. No buyer column, no money column.
-- Alerts are notifications (slice 6.4): no new table.
--
-- Safe on the live database: one column grant.

grant select (expected_ship_date) on app.etsy_order_items to htco_worker;
