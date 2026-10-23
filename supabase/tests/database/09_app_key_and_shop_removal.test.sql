-- Replacing the Etsy app key and removing a shop: the app key fingerprint is 16 lower-case hex
-- characters, readable by members like the other card columns and writable by no client role;
-- deleting a shop leaves nothing of it behind (tokens, probe results, orders, items, refunds,
-- buyers, sync state, grants) while other shops keep theirs; no client role may delete a shop.
begin;
create extension if not exists pgtap with schema extensions;
select plan(12);

insert into auth.users (id, email) values
  ('91111111-0000-0000-0000-000000000001', 'owner-g@example.test'),
  ('91111111-0000-0000-0000-000000000002', 'support-g@example.test');
insert into app.organizations (id, name) values
  ('9a000000-0000-0000-0000-000000000000', 'Organization G');
insert into app.memberships (id, organization_id, user_id, role) values
  ('9e000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', '91111111-0000-0000-0000-000000000001', 'owner'),
  ('9e000000-0000-0000-0000-000000000002', '9a000000-0000-0000-0000-000000000000', '91111111-0000-0000-0000-000000000002', 'support');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('9c000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', 'Shop G1', 'etsy', 'active'),
  ('9c000000-0000-0000-0000-000000000002', '9a000000-0000-0000-0000-000000000000', 'Shop G2', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('9e100000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', '9e000000-0000-0000-0000-000000000002', '9c000000-0000-0000-0000-000000000001'),
  ('9e100000-0000-0000-0000-000000000002', '9a000000-0000-0000-0000-000000000000', '9e000000-0000-0000-0000-000000000002', '9c000000-0000-0000-0000-000000000002');
insert into app.shop_integrations
  (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id, wrapped_data_key,
   access_token_ciphertext, refresh_token_ciphertext, scopes, access_expires_at,
   refresh_expires_at, status, app_key_fingerprint)
values
  ('9c100000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000',
   '9c000000-0000-0000-0000-000000000001', 'etsy', 901, 911, 'key-g', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected', '0123456789abcdef'),
  ('9c100000-0000-0000-0000-000000000002', '9a000000-0000-0000-0000-000000000000',
   '9c000000-0000-0000-0000-000000000002', 'etsy', 902, 912, 'key-g', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected', null);
insert into app.shop_capabilities
  (id, organization_id, shop_id, capability, state, detail, checked_at)
values
  ('9c200000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000',
   '9c000000-0000-0000-0000-000000000001', 'shop_details', 'available', 'read_ok', now());
insert into app.etsy_orders (
  organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
  grand_total_amount, grand_total_divisor, grand_total_currency,
  subtotal_amount, subtotal_divisor, subtotal_currency,
  shipping_amount, shipping_divisor, shipping_currency,
  discount_amount, discount_divisor, discount_currency,
  country_code, etsy_created_at, etsy_updated_at, synced_at
) values
  ('9a000000-0000-0000-0000-000000000000', '9c000000-0000-0000-0000-000000000001', 901, 1, 'paid', true, false,
   2599, 100, 'USD', 2000, 100, 'USD', 599, 100, 'USD', 0, 100, 'USD', 'US', now(), now(), now()),
  ('9a000000-0000-0000-0000-000000000000', '9c000000-0000-0000-0000-000000000002', 902, 1, 'paid', true, false,
   2599, 100, 'USD', 2000, 100, 'USD', 599, 100, 'USD', 0, 100, 'USD', 'US', now(), now(), now());
insert into app.etsy_order_buyers (organization_id, shop_id, receipt_id, buyer_name, address_city) values
  ('9a000000-0000-0000-0000-000000000000', '9c000000-0000-0000-0000-000000000001', 1, 'Fake Buyer G', 'Fakecity');
insert into app.etsy_order_refunds (organization_id, shop_id, receipt_id, position, amount, divisor, currency, refunded_at, reason) values
  ('9a000000-0000-0000-0000-000000000000', '9c000000-0000-0000-0000-000000000001', 1, 0, 500, 100, 'USD', now(), 'damaged');

select has_column('app', 'shop_integrations', 'app_key_fingerprint', 'the app key fingerprint exists');
select throws_ok(
  $$ update app.shop_integrations set app_key_fingerprint = 'THE-KEY-ITSELF'
     where id = '9c100000-0000-0000-0000-000000000002' $$,
  '23514', null, 'only a 16-character hex fingerprint may be stored, never a key'
);
select ok(has_column_privilege('authenticated', 'app.shop_integrations', 'app_key_fingerprint', 'select'),
  'members may read the fingerprint (RLS decides which rows)');
select ok(not has_column_privilege('authenticated', 'app.shop_integrations', 'app_key_fingerprint', 'update'),
  'no member may change it');
select ok(not has_table_privilege('authenticated', 'app.shops', 'delete'),
  'no member may delete a shop: removal runs as the API');
select ok(not has_table_privilege('service_role', 'app.shop_integrations', 'select'),
  'service_role still cannot read the integrations');

-- Removing Shop G1 the way the API does it, in one transaction.
delete from app.shop_integrations where shop_id = '9c000000-0000-0000-0000-000000000001';
delete from app.shop_capabilities where shop_id = '9c000000-0000-0000-0000-000000000001';
delete from app.etsy_order_sync_state where shop_id = '9c000000-0000-0000-0000-000000000001';
delete from app.etsy_orders where shop_id = '9c000000-0000-0000-0000-000000000001';
delete from app.shop_access where shop_id = '9c000000-0000-0000-0000-000000000001';
delete from app.shops where id = '9c000000-0000-0000-0000-000000000001';

select is((select count(*)::int from app.etsy_order_buyers where shop_id = '9c000000-0000-0000-0000-000000000001'), 0,
  'the removed shop''s buyer rows are gone with its orders');
select is((select count(*)::int from app.etsy_order_refunds where shop_id = '9c000000-0000-0000-0000-000000000001'), 0,
  'the removed shop''s refunds are gone');
select is((select count(*)::int from app.etsy_orders where shop_id = '9c000000-0000-0000-0000-000000000002'), 1,
  'another shop keeps its orders');
select is((select count(*)::int from app.shop_access where shop_id = '9c000000-0000-0000-0000-000000000002'), 1,
  'another shop keeps its grants');

-- A shop deleted directly still takes everything with it (the cascades).
delete from app.shops where id = '9c000000-0000-0000-0000-000000000002';
select is((select count(*)::int from app.shop_integrations where organization_id = '9a000000-0000-0000-0000-000000000000'), 0,
  'deleting a shop cascades to its integration (and its tokens)');
select is((select count(*)::int from app.etsy_orders where organization_id = '9a000000-0000-0000-0000-000000000000'), 0,
  'deleting a shop cascades to its stored orders');

select * from finish();
rollback;
