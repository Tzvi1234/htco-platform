-- The worker's purge of a disconnected shop's stored order history (post-merge review of
-- 2026-10-23): the worker may delete its job's organization's orders and sync state, and reads
-- no buyer row, no other column and no other organization's rows.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(9);

insert into app.organizations (id, name) values
  ('9a000000-0000-0000-0000-000000000000', 'Organization P'),
  ('9b000000-0000-0000-0000-000000000000', 'Organization Q');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('9c000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', 'Shop P', 'etsy', 'disconnected'),
  ('9d000000-0000-0000-0000-000000000001', '9b000000-0000-0000-0000-000000000000', 'Shop Q', 'etsy', 'disconnected');
insert into app.etsy_orders
  (organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
   grand_total_amount, grand_total_divisor, grand_total_currency,
   subtotal_amount, subtotal_divisor, subtotal_currency,
   shipping_amount, shipping_divisor, shipping_currency,
   discount_amount, discount_divisor, discount_currency,
   etsy_created_at, etsy_updated_at, synced_at)
values
  ('9a000000-0000-0000-0000-000000000000', '9c000000-0000-0000-0000-000000000001', 901, 9001,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now()),
  ('9b000000-0000-0000-0000-000000000000', '9d000000-0000-0000-0000-000000000001', 902, 9002,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now());
insert into app.etsy_order_buyers (organization_id, shop_id, receipt_id, buyer_name) values
  ('9a000000-0000-0000-0000-000000000000', '9c000000-0000-0000-0000-000000000001', 9001, 'Fake P'),
  ('9b000000-0000-0000-0000-000000000000', '9d000000-0000-0000-0000-000000000001', 9002, 'Fake Q');
insert into app.etsy_order_sync_state (shop_id, organization_id, etsy_shop_id) values
  ('9c000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', 901),
  ('9d000000-0000-0000-0000-000000000001', '9b000000-0000-0000-0000-000000000000', 902);

select ok(has_table_privilege('htco_worker', 'app.etsy_orders', 'delete'),
  'the worker may delete stored orders');
select ok(has_table_privilege('htco_worker', 'app.etsy_order_sync_state', 'delete'),
  'the worker may delete the sync state');
select ok(not has_table_privilege('htco_worker', 'app.etsy_order_buyers', 'select'),
  'the worker never reads buyer rows');
select ok(not has_column_privilege('htco_worker', 'app.etsy_orders', 'grand_total_amount', 'select'),
  'the worker reads no order column beyond the tenant keys');

set local role htco_worker;
select set_config('htco.organization_id', '9a000000-0000-0000-0000-000000000000', true);

select throws_ok(
  $$select buyer_name from app.etsy_order_buyers$$,
  '42501', null, 'the worker cannot read buyer names'
);
select is(
  (with gone as (
     delete from app.etsy_orders where shop_id = '9d000000-0000-0000-0000-000000000001' returning 1
   ) select count(*)::integer from gone),
  0, 'another organization''s orders are not the worker''s to delete'
);
select is(
  (with gone as (
     delete from app.etsy_orders where shop_id = '9c000000-0000-0000-0000-000000000001' returning 1
   ) select count(*)::integer from gone),
  1, 'the job''s organization''s orders are deleted'
);

reset role;
select is(
  (select count(*)::integer from app.etsy_order_buyers
   where shop_id = '9c000000-0000-0000-0000-000000000001'),
  0, 'the buyer rows went with their order'
);
select is(
  (select count(*)::integer from app.etsy_orders
   where shop_id = '9d000000-0000-0000-0000-000000000001'),
  1, 'the other organization''s order is kept'
);

select * from finish();
rollback;
