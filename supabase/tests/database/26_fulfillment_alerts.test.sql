-- Ship-by alerts (slice 6.9): the worker reads the ship-by date and still no money or title.
begin;
create extension if not exists pgtap with schema extensions;
select plan(3);

select ok(has_column_privilege('htco_worker', 'app.etsy_order_items', 'expected_ship_date', 'select'),
  'the worker reads the ship-by date');
select ok(not has_column_privilege('htco_worker', 'app.etsy_order_items', 'unit_price_amount', 'select'),
  'the worker still reads no price');
select ok(not has_column_privilege('htco_worker', 'app.etsy_order_items', 'title', 'select'),
  'the worker still reads no item title');

select * from finish();
rollback;
