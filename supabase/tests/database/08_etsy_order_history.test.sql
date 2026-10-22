-- The stored order history: RLS on every table, writes only by the API's login role, buyer rows
-- only for buyer-data roles (Owner, Admin; Operations and Support with access), no contact
-- columns, and the purge of shops disconnected past their date.
begin;
create extension if not exists pgtap with schema extensions;
select plan(17);

insert into auth.users (id, email) values
  ('81111111-0000-0000-0000-000000000001', 'owner-e@example.test'),
  ('81111111-0000-0000-0000-000000000002', 'support-e@example.test'),
  ('81111111-0000-0000-0000-000000000003', 'finance-e@example.test'),
  ('82222222-0000-0000-0000-000000000001', 'owner-f@example.test');
insert into app.organizations (id, name) values
  ('8a000000-0000-0000-0000-000000000000', 'Organization E'),
  ('8b000000-0000-0000-0000-000000000000', 'Organization F');
insert into app.memberships (id, organization_id, user_id, role) values
  ('8e000000-0000-0000-0000-000000000001', '8a000000-0000-0000-0000-000000000000', '81111111-0000-0000-0000-000000000001', 'owner'),
  ('8e000000-0000-0000-0000-000000000002', '8a000000-0000-0000-0000-000000000000', '81111111-0000-0000-0000-000000000002', 'support'),
  ('8e000000-0000-0000-0000-000000000003', '8a000000-0000-0000-0000-000000000000', '81111111-0000-0000-0000-000000000003', 'finance'),
  ('8f000000-0000-0000-0000-000000000001', '8b000000-0000-0000-0000-000000000000', '82222222-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('8c000000-0000-0000-0000-000000000001', '8a000000-0000-0000-0000-000000000000', 'Shop E', 'etsy', 'active'),
  ('8d000000-0000-0000-0000-000000000001', '8b000000-0000-0000-0000-000000000000', 'Shop F', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('8e100000-0000-0000-0000-000000000002', '8a000000-0000-0000-0000-000000000000', '8e000000-0000-0000-0000-000000000002', '8c000000-0000-0000-0000-000000000001'),
  ('8e100000-0000-0000-0000-000000000003', '8a000000-0000-0000-0000-000000000000', '8e000000-0000-0000-0000-000000000003', '8c000000-0000-0000-0000-000000000001');

insert into app.etsy_orders (
  organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
  grand_total_amount, grand_total_divisor, grand_total_currency,
  subtotal_amount, subtotal_divisor, subtotal_currency,
  shipping_amount, shipping_divisor, shipping_currency,
  discount_amount, discount_divisor, discount_currency,
  country_code, etsy_created_at, etsy_updated_at, synced_at
) values
  ('8a000000-0000-0000-0000-000000000000', '8c000000-0000-0000-0000-000000000001', 801, 1, 'paid', true, false,
   2599, 100, 'USD', 2000, 100, 'USD', 599, 100, 'USD', 0, 100, 'USD', 'US', now(), now(), now()),
  ('8b000000-0000-0000-0000-000000000000', '8d000000-0000-0000-0000-000000000001', 802, 1, 'paid', true, false,
   2599, 100, 'USD', 2000, 100, 'USD', 599, 100, 'USD', 0, 100, 'USD', 'US', now(), now(), now());
insert into app.etsy_order_buyers (organization_id, shop_id, receipt_id, buyer_name, address_city) values
  ('8a000000-0000-0000-0000-000000000000', '8c000000-0000-0000-0000-000000000001', 1, 'Fake Buyer E', 'Fakecity'),
  ('8b000000-0000-0000-0000-000000000000', '8d000000-0000-0000-0000-000000000001', 1, 'Fake Buyer F', 'Fakecity');
insert into app.etsy_order_refunds (organization_id, shop_id, receipt_id, position, amount, divisor, currency, refunded_at, reason) values
  ('8a000000-0000-0000-0000-000000000000', '8c000000-0000-0000-0000-000000000001', 1, 0, 500, 100, 'USD', now(), 'damaged');

select ok(
  (select bool_and(relrowsecurity) from pg_class where oid in (
    'app.etsy_orders'::regclass, 'app.etsy_order_items'::regclass, 'app.etsy_order_refunds'::regclass,
    'app.etsy_order_buyers'::regclass, 'app.etsy_order_sync_state'::regclass)),
  'every order history table has RLS enabled'
);
select ok(not has_table_privilege('authenticated', 'app.etsy_orders', 'insert, update, delete'),
  'authenticated cannot write orders');
select ok(not has_table_privilege('authenticated', 'app.etsy_order_buyers', 'insert, update, delete'),
  'authenticated cannot write buyer rows');
select ok(not has_table_privilege('anon', 'app.etsy_order_buyers', 'select'), 'anon cannot read buyer rows');
select ok(not has_table_privilege('service_role', 'app.etsy_order_buyers', 'select'),
  'service_role cannot read buyer rows either');
select is_empty(
  $$ select column_name from information_schema.columns
     where table_schema = 'app' and table_name like 'etsy_order%'
       and (column_name like '%email%' or column_name like '%phone%'
            or column_name like '%message%' or column_name like '%gift%') $$,
  'no column for email, phone, messages or gift messages'
);
select ok(not has_function_privilege('authenticated', 'private.purge_disconnected_etsy_orders(timestamptz)', 'execute'),
  'members cannot run the purge');

-- Owner E: their organization's orders and buyer rows.
select set_config('request.jwt.claims', '{"sub":"81111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select results_eq($$ select etsy_shop_id from app.etsy_orders $$, $$ values (801::bigint) $$,
  'an Owner sees the orders of their own organization only');
select results_eq($$ select buyer_name from app.etsy_order_buyers $$, $$ values ('Fake Buyer E'::text) $$,
  'an Owner sees the buyer rows of their own organization only');
reset role;

-- Support E with access: buyer data allowed.
select set_config('request.jwt.claims', '{"sub":"81111111-0000-0000-0000-000000000002","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::int from app.etsy_order_buyers), 1, 'Support with access sees buyer rows');
reset role;

-- Finance E with access: orders and refunds, no buyer rows.
select set_config('request.jwt.claims', '{"sub":"81111111-0000-0000-0000-000000000003","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::int from app.etsy_orders), 1, 'Finance with access sees orders');
select is((select count(*)::int from app.etsy_order_refunds), 1, 'Finance with access sees refunds and reasons');
select is((select count(*)::int from app.etsy_order_buyers), 0, 'Finance sees no buyer row');
reset role;

-- The purge: only shops disconnected past their date.
insert into app.shop_integrations
  (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, status, disconnected_at, purge_after)
values
  ('8c100000-0000-0000-0000-000000000001', '8a000000-0000-0000-0000-000000000000',
   '8c000000-0000-0000-0000-000000000001', 'etsy', 801, 901, 'disconnected',
   now() - interval '40 days', now() - interval '10 days'),
  ('8d100000-0000-0000-0000-000000000001', '8b000000-0000-0000-0000-000000000000',
   '8d000000-0000-0000-0000-000000000001', 'etsy', 802, 902, 'disconnected',
   now() - interval '1 day', now() + interval '29 days');
select is(private.purge_disconnected_etsy_orders(now()), 1, 'the purge deletes the due shop''s orders');
select is((select count(*)::int from app.etsy_order_buyers where shop_id = '8c000000-0000-0000-0000-000000000001'), 0,
  'buyer rows go with their orders');
select is((select count(*)::int from app.etsy_order_refunds where shop_id = '8c000000-0000-0000-0000-000000000001'), 0,
  'refunds go with their orders');
select is((select count(*)::int from app.etsy_orders where shop_id = '8d000000-0000-0000-0000-000000000001'), 1,
  'a shop not yet past its date keeps its orders');

select * from finish();
rollback;
