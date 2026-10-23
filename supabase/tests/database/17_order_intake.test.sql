-- Etsy Order Intake's store (slice 5.4): the order machine in the database, and the worker's
-- least-privilege writes in its job's organization (no money column, no buyer value readable).
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(14);

insert into app.organizations (id, name) values
  ('7a000000-0000-0000-0000-000000000000', 'Organization I'),
  ('7b000000-0000-0000-0000-000000000000', 'Organization J');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('7c000000-0000-0000-0000-000000000001', '7a000000-0000-0000-0000-000000000000', 'Shop I', 'etsy', 'active'),
  ('7d000000-0000-0000-0000-000000000001', '7b000000-0000-0000-0000-000000000000', 'Shop J', 'etsy', 'active');
insert into app.etsy_orders
  (organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
   grand_total_amount, grand_total_divisor, grand_total_currency,
   subtotal_amount, subtotal_divisor, subtotal_currency,
   shipping_amount, shipping_divisor, shipping_currency,
   discount_amount, discount_divisor, discount_currency,
   etsy_created_at, etsy_updated_at, synced_at, order_state)
values
  ('7a000000-0000-0000-0000-000000000000', '7c000000-0000-0000-0000-000000000001', 701, 7001,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now(), 'awaiting_shipment'),
  ('7b000000-0000-0000-0000-000000000000', '7d000000-0000-0000-0000-000000000001', 702, 7002,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now(), 'awaiting_shipment');

-- The machine --------------------------------------------------------------------------------
select lives_ok(
  $$insert into app.order_status_events
      (id, organization_id, shop_id, receipt_id, from_state, to_state, source, observed_at,
       etsy_updated_at, receipt_sha256)
    values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
            '7c000000-0000-0000-0000-000000000001', 7001, 'awaiting_shipment', 'shipped',
            'webhook', now(), now(), decode(repeat('ab', 32), 'hex'))$$,
  'an allowed transition is recorded'
);
select throws_ok(
  $$insert into app.order_status_events
      (id, organization_id, shop_id, receipt_id, from_state, to_state, source, observed_at,
       etsy_updated_at, receipt_sha256)
    values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
            '7c000000-0000-0000-0000-000000000001', 7001, 'shipped', 'awaiting_shipment',
            'webhook', now(), now(), decode(repeat('ab', 32), 'hex'))$$,
  '23514', null, 'a transition backwards is refused'
);
select throws_ok(
  $$insert into app.order_status_events
      (id, organization_id, shop_id, receipt_id, from_state, to_state, source, observed_at,
       etsy_updated_at, receipt_sha256)
    values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
            '7c000000-0000-0000-0000-000000000001', 7001, null, 'shipped',
            'webhook', now(), now(), decode(repeat('ab', 32), 'hex'))$$,
  '23514', null, 'an order starts unpaid or paid'
);
select throws_ok(
  $$insert into app.financial_entries
      (id, organization_id, shop_id, receipt_id, kind, position, amount, currency, source,
       occurred_at)
    values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
            '7c000000-0000-0000-0000-000000000001', 7001, 'fee', 0, 1, 'USD', 'etsy_receipt',
            now())$$,
  '23514', null, 'only revenue and refund lines exist in Phase 5'
);

-- The worker's privileges --------------------------------------------------------------------
select ok(has_table_privilege('htco_worker', 'app.etsy_orders', 'insert, update'),
  'the worker writes orders');
select ok(not has_column_privilege('htco_worker', 'app.etsy_orders', 'grand_total_amount', 'select'),
  'the worker still reads no money column');
select ok(not has_column_privilege('htco_worker', 'app.etsy_order_buyers', 'buyer_name', 'select'),
  'the worker cannot read a buyer name');
select ok(not has_column_privilege('htco_worker', 'app.etsy_order_buyers', 'address_first_line', 'select'),
  'the worker cannot read an address');
select ok(has_table_privilege('htco_worker', 'app.etsy_order_buyers', 'insert, delete'),
  'the worker replaces the buyer row with its receipt');
select ok(not has_table_privilege('authenticated', 'app.order_status_events', 'insert, update, delete'),
  'members never write the order history');

set local role htco_worker;
select set_config('htco.organization_id', '7a000000-0000-0000-0000-000000000000', true);

select is(
  (select order_state from app.etsy_orders where receipt_id = 7001), 'awaiting_shipment',
  'the worker reads the order state of its organization'
);
select is(
  (select count(*)::integer from app.etsy_orders where receipt_id = 7002), 0,
  'the worker sees no order of another organization'
);
select throws_ok(
  $$select buyer_name from app.etsy_order_buyers$$,
  '42501', null, 'buyer names stay unreadable to the worker'
);
select throws_ok(
  $$insert into app.order_status_events
      (id, organization_id, shop_id, receipt_id, from_state, to_state, source, observed_at,
       etsy_updated_at, receipt_sha256)
    values (gen_random_uuid(), '7b000000-0000-0000-0000-000000000000',
            '7d000000-0000-0000-0000-000000000001', 7002, 'awaiting_shipment', 'shipped',
            'webhook', now(), now(), decode(repeat('ab', 32), 'hex'))$$,
  '42501', null, 'the worker records no transition of another organization'
);

reset role;
select * from finish();
rollback;
