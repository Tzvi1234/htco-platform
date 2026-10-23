-- The order automations' tables (slice 5.11): counters that never go negative, one application
-- per receipt, the new drift kind and source, and tenant isolation for members and the worker.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(21);

insert into auth.users (id, email) values
  ('22e00000-0000-0000-0000-000000000001', 'owner-m@example.test'),
  ('22f00000-0000-0000-0000-000000000001', 'owner-n@example.test');
insert into app.organizations (id, name) values
  ('22a00000-0000-0000-0000-000000000000', 'Organization M'),
  ('22b00000-0000-0000-0000-000000000000', 'Organization N');
insert into app.memberships (id, organization_id, user_id, role) values
  ('22d00000-0000-0000-0000-000000000001', '22a00000-0000-0000-0000-000000000000', '22e00000-0000-0000-0000-000000000001', 'owner'),
  ('22d00000-0000-0000-0000-000000000002', '22b00000-0000-0000-0000-000000000000', '22f00000-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('22c00000-0000-0000-0000-000000000001', '22a00000-0000-0000-0000-000000000000', 'Shop M', 'etsy', 'active'),
  ('22c00000-0000-0000-0000-000000000002', '22b00000-0000-0000-0000-000000000000', 'Shop N', 'etsy', 'active');
insert into app.etsy_orders
  (organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
   grand_total_amount, grand_total_divisor, grand_total_currency,
   subtotal_amount, subtotal_divisor, subtotal_currency,
   shipping_amount, shipping_divisor, shipping_currency,
   discount_amount, discount_divisor, discount_currency,
   etsy_created_at, etsy_updated_at, synced_at, order_state)
values
  ('22a00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 2201, 22001,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now(), 'awaiting_shipment'),
  ('22b00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000002', 2202, 22002,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now(), 'awaiting_shipment'),
  ('22a00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 2201, 22003,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now(), 'awaiting_shipment');
insert into app.product_sales_counters (organization_id, shop_id, listing_id, units, orders) values
  ('22a00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 7, 2, 1),
  ('22b00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000002', 7, 5, 1);
insert into app.product_sales_applications (organization_id, shop_id, receipt_id, counted, applied_at) values
  ('22a00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 22001, '{"7": 2}', now());

-- Structure and rules.
select ok((select relrowsecurity from pg_class where oid = 'app.product_sales_counters'::regclass),
  'counters have RLS enabled');
select ok((select relrowsecurity from pg_class where oid = 'app.product_sales_applications'::regclass),
  'applications have RLS enabled');
select throws_ok(
  $$ update app.product_sales_counters set units = -1
     where shop_id = '22c00000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a counter never goes negative');
select throws_ok(
  $$ insert into app.product_sales_applications (organization_id, shop_id, receipt_id, applied_at)
     values ('22a00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 22001, now()) $$,
  '23505', null, 'one application per receipt');
select throws_ok(
  $$ insert into app.product_sales_applications (organization_id, shop_id, receipt_id, applied_at)
     values ('22a00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 99999, now()) $$,
  '23503', null, 'an application belongs to a stored order');
select throws_ok(
  $$ insert into app.product_sales_applications (organization_id, shop_id, receipt_id, applied_at)
     values ('22b00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 22003, now()) $$,
  '23503', null, 'an application belongs to its order''s organization');
select lives_ok(
  $$ insert into app.sync_drifts (id, organization_id, shop_id, receipt_id, kind, stored, observed,
       etsy_updated_at, source, detected_at)
     values (gen_random_uuid(), '22a00000-0000-0000-0000-000000000000',
             '22c00000-0000-0000-0000-000000000001', 22001, 'totals', '{"grand_total": "stored"}',
             '{"grand_total": "differs"}', now(), 'verification', now()) $$,
  'verification records a totals drift');
select throws_ok(
  $$ insert into app.sync_drifts (id, organization_id, shop_id, receipt_id, kind, stored, observed,
       etsy_updated_at, source, detected_at)
     values (gen_random_uuid(), '22a00000-0000-0000-0000-000000000000',
             '22c00000-0000-0000-0000-000000000001', 22001, 'price', null, '{}', now(),
             'verification', now()) $$,
  '23514', null, 'an unknown drift kind is refused');
select ok(not has_column_privilege('htco_worker', 'app.etsy_orders', 'grand_total_amount', 'select'),
  'the worker still reads no money column');
select ok(has_function_privilege('htco_worker',
  'app.worker_order_grand_total_matches(uuid, bigint, bigint, integer, text)', 'execute'),
  'the worker asks whether the stored total matches');
select ok(not has_function_privilege('authenticated',
  'app.worker_order_grand_total_matches(uuid, bigint, bigint, integer, text)', 'execute'),
  'users cannot ask it');
select ok(not has_schema_privilege('htco_worker', 'private', 'usage'),
  'the worker still has no usage on schema private');
select ok(not has_table_privilege('authenticated', 'app.product_sales_counters', 'insert, update, delete'),
  'users only read the counters');

-- Members: their own organization's counters only; applications are the worker's.
select set_config('request.jwt.claims', '{"sub":"22e00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select units::int from app.product_sales_counters $$, $$ values (2) $$,
  'Owner M sees organization M''s counters only');
select throws_ok($$ select count(*) from app.product_sales_applications $$, '42501', null,
  'users do not read the applications');
reset role;

-- The worker: its job's organization only.
set local role htco_worker;
select set_config('htco.organization_id', '22b00000-0000-0000-0000-000000000000', true);
select results_eq($$ select units::int from app.product_sales_counters $$, $$ values (5) $$,
  'the worker sees its job''s organization''s counters only');
select is_empty($$ select receipt_id from app.product_sales_applications $$,
  'and none of another organization''s applications');
select throws_ok(
  $$ insert into app.product_sales_applications (organization_id, shop_id, receipt_id, applied_at)
     values ('22b00000-0000-0000-0000-000000000000', '22c00000-0000-0000-0000-000000000001', 22003, now()) $$,
  '23503', null, 'the worker cannot bind its organization to another organization''s order');
select is(app.worker_order_grand_total_matches('22c00000-0000-0000-0000-000000000002', 22002, 100, 100, 'USD'),
  true, 'the stored total matches the same total');
select is(app.worker_order_grand_total_matches('22c00000-0000-0000-0000-000000000001', 22001, 100, 100, 'USD'),
  null, 'another organization''s order answers nothing');
reset role;

select * from finish();
rollback;
