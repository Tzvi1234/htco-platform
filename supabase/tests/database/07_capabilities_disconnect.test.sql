-- Capability probes (slice 4.5) and disconnect (slice 4.7): probe results readable by Owner and
-- Admin of the organization only, written only by the API's login role; detail codes cannot carry
-- text; disconnect dates only on disconnected integrations.
begin;
create extension if not exists pgtap with schema extensions;
select plan(18);

insert into auth.users (id, email) values
  ('71111111-0000-0000-0000-000000000001', 'owner-c@example.test'),
  ('71111111-0000-0000-0000-000000000002', 'support-c@example.test'),
  ('72222222-0000-0000-0000-000000000001', 'owner-d@example.test');
insert into app.organizations (id, name) values
  ('7a000000-0000-0000-0000-000000000000', 'Organization C'),
  ('7b000000-0000-0000-0000-000000000000', 'Organization D');
insert into app.memberships (id, organization_id, user_id, role) values
  ('7e000000-0000-0000-0000-000000000001', '7a000000-0000-0000-0000-000000000000', '71111111-0000-0000-0000-000000000001', 'owner'),
  ('7e000000-0000-0000-0000-000000000002', '7a000000-0000-0000-0000-000000000000', '71111111-0000-0000-0000-000000000002', 'support'),
  ('7f000000-0000-0000-0000-000000000001', '7b000000-0000-0000-0000-000000000000', '72222222-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('7c000000-0000-0000-0000-000000000001', '7a000000-0000-0000-0000-000000000000', 'Shop C', 'etsy', 'active'),
  ('7d000000-0000-0000-0000-000000000001', '7b000000-0000-0000-0000-000000000000', 'Shop D', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('7e100000-0000-0000-0000-000000000002', '7a000000-0000-0000-0000-000000000000', '7e000000-0000-0000-0000-000000000002', '7c000000-0000-0000-0000-000000000001');
insert into app.shop_capabilities
  (id, organization_id, shop_id, capability, state, detail, checked_at)
values
  ('7c200000-0000-0000-0000-000000000001', '7a000000-0000-0000-0000-000000000000',
   '7c000000-0000-0000-0000-000000000001', 'shop_details', 'available', 'read_ok', now()),
  ('7d200000-0000-0000-0000-000000000001', '7b000000-0000-0000-0000-000000000000',
   '7d000000-0000-0000-0000-000000000001', 'shop_details', 'unknown', 'auth_failed', now());
insert into app.shop_integrations
  (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id, wrapped_data_key,
   access_token_ciphertext, refresh_token_ciphertext, scopes, access_expires_at,
   refresh_expires_at, status)
values
  ('7c100000-0000-0000-0000-000000000001', '7a000000-0000-0000-0000-000000000000',
   '7c000000-0000-0000-0000-000000000001', 'etsy', 701, 801, 'key-a', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected');

select has_table('app', 'shop_capabilities', 'app.shop_capabilities exists');
select ok(
  (select relrowsecurity from pg_class where oid = 'app.shop_capabilities'::regclass),
  'shop_capabilities has RLS enabled'
);
select ok(not has_table_privilege('authenticated', 'app.shop_capabilities', 'insert, update, delete'),
  'authenticated cannot write probe results');
select ok(not has_table_privilege('anon', 'app.shop_capabilities', 'select'),
  'anon cannot read probe results');
select ok(not has_table_privilege('service_role', 'app.shop_capabilities', 'select'),
  'service_role cannot read probe results either');
select ok(has_column_privilege('authenticated', 'app.shop_integrations', 'purge_after', 'select'),
  'authenticated may read the purge date (RLS decides which rows)');

-- Owner C: their organization's results only.
select set_config('request.jwt.claims', '{"sub":"71111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select results_eq(
  $$ select shop_id, capability, state, detail from app.shop_capabilities $$,
  $$ values ('7c000000-0000-0000-0000-000000000001'::uuid, 'shop_details'::text,
             'available'::text, 'read_ok'::text) $$,
  'an Owner sees the probe results of their own organization only'
);
select throws_ok(
  $$ update app.shop_capabilities set state = 'available' $$,
  '42501', null, 'an Owner cannot change a probe result'
);
reset role;

-- Support C has access to the shop, but connection details are for Owner and Admin.
select set_config('request.jwt.claims', '{"sub":"71111111-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is_empty(
  $$ select id from app.shop_capabilities $$,
  'a Support member sees no probe result'
);
reset role;

-- Constraints.
select throws_ok(
  $$ insert into app.shop_capabilities (id, organization_id, shop_id, capability, state, detail, checked_at)
     values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
       '7c000000-0000-0000-0000-000000000001', 'shop_details', 'unknown', 'x', now()) $$,
  '23505', null, 'one result per shop and capability'
);
select throws_ok(
  $$ insert into app.shop_capabilities (id, organization_id, shop_id, capability, state, detail, checked_at)
     values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
       '7d000000-0000-0000-0000-000000000001', 'webhooks', 'unknown', 'x', now()) $$,
  '23503', null, 'the shop must belong to the same organization'
);
select throws_ok(
  $$ insert into app.shop_capabilities (id, organization_id, shop_id, capability, state, detail, checked_at)
     values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
       '7c000000-0000-0000-0000-000000000001', 'webhooks', 'unknown', '1 Main Street, Springfield', now()) $$,
  '23514', null, 'a detail is a code, never free text such as an address'
);
select throws_ok(
  $$ insert into app.shop_capabilities (id, organization_id, shop_id, capability, state, detail, checked_at)
     values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
       '7c000000-0000-0000-0000-000000000001', 'create_listing', 'unknown', 'x', now()) $$,
  '23514', null, 'only the probed capabilities exist'
);
select throws_ok(
  $$ insert into app.shop_capabilities (id, organization_id, shop_id, capability, state, detail, checked_at)
     values (gen_random_uuid(), '7a000000-0000-0000-0000-000000000000',
       '7c000000-0000-0000-0000-000000000001', 'webhooks', 'maybe', 'x', now()) $$,
  '23514', null, 'a state is available, unavailable or unknown'
);
select throws_ok(
  $$ update app.shop_integrations set purge_after = now() + interval '30 days',
       disconnected_at = now()
     where id = '7c100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a connected integration has no disconnect or purge date'
);
select lives_ok(
  $$ update app.shop_integrations
     set status = 'disconnected', key_id = null, wrapped_data_key = null,
         access_token_ciphertext = null, refresh_token_ciphertext = null,
         access_expires_at = null, refresh_expires_at = null,
         disconnected_at = now(), purge_after = now() + interval '30 days'
     where id = '7c100000-0000-0000-0000-000000000001' $$,
  'disconnecting keeps the row with its disconnect and purge dates'
);
select throws_ok(
  $$ update app.shop_integrations set purge_after = disconnected_at - interval '1 day'
     where id = '7c100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'the purge date comes after the disconnect'
);
select throws_ok(
  $$ update app.shop_integrations set disconnected_at = null
     where id = '7c100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a purge date needs the disconnect time'
);

select * from finish();
rollback;
