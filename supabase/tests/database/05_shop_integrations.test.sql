-- Shop integrations (slice 4.2): sealed Etsy tokens. Only the API's login role reads the sealed
-- columns; an Owner or Admin reads the rest of their organization's rows (for the shop card).
begin;
create extension if not exists pgtap with schema extensions;
select plan(22);

insert into auth.users (id, email) values
  ('51111111-0000-0000-0000-000000000001', 'owner-v@example.test'),
  ('51111111-0000-0000-0000-000000000002', 'support-v@example.test'),
  ('52222222-0000-0000-0000-000000000001', 'owner-w@example.test');
insert into app.organizations (id, name) values
  ('5a000000-0000-0000-0000-000000000000', 'Organization V'),
  ('5b000000-0000-0000-0000-000000000000', 'Organization W');
insert into app.memberships (id, organization_id, user_id, role) values
  ('5e000000-0000-0000-0000-000000000001', '5a000000-0000-0000-0000-000000000000', '51111111-0000-0000-0000-000000000001', 'owner'),
  ('5e000000-0000-0000-0000-000000000002', '5a000000-0000-0000-0000-000000000000', '51111111-0000-0000-0000-000000000002', 'support'),
  ('5f000000-0000-0000-0000-000000000001', '5b000000-0000-0000-0000-000000000000', '52222222-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('5c000000-0000-0000-0000-000000000001', '5a000000-0000-0000-0000-000000000000', 'Shop V', 'etsy', 'active'),
  ('5d000000-0000-0000-0000-000000000001', '5b000000-0000-0000-0000-000000000000', 'Shop W', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('5e100000-0000-0000-0000-000000000002', '5a000000-0000-0000-0000-000000000000', '5e000000-0000-0000-0000-000000000002', '5c000000-0000-0000-0000-000000000001');
insert into app.shop_integrations
  (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id, wrapped_data_key,
   access_token_ciphertext, refresh_token_ciphertext, scopes, access_expires_at,
   refresh_expires_at, status)
values
  ('5c100000-0000-0000-0000-000000000001', '5a000000-0000-0000-0000-000000000000',
   '5c000000-0000-0000-0000-000000000001', 'etsy', 101, 201, 'key-a', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected'),
  ('5d100000-0000-0000-0000-000000000001', '5b000000-0000-0000-0000-000000000000',
   '5d000000-0000-0000-0000-000000000001', 'etsy', 102, 202, 'key-a', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected');

select has_table('app', 'shop_integrations', 'app.shop_integrations exists');
select ok(
  (select relrowsecurity from pg_class where oid = 'app.shop_integrations'::regclass),
  'shop_integrations has RLS enabled'
);
select col_type_is('app', 'shop_integrations', 'access_token_ciphertext', 'bytea',
  'the access token is stored sealed (bytes)');
select col_type_is('app', 'shop_integrations', 'refresh_token_ciphertext', 'bytea',
  'the refresh token is stored sealed (bytes)');
select is(
  (select count(*)::int from information_schema.columns
   where table_schema = 'app' and table_name = 'shop_integrations'
     and column_name like '%token%' and data_type <> 'bytea'),
  0, 'no token column holds text: there is no plaintext token column'
);

-- Grants: nobody but the login role reads the sealed columns or the key ID.
select ok(not has_column_privilege('authenticated', 'app.shop_integrations', 'access_token_ciphertext', 'select'),
  'authenticated cannot read the access token');
select ok(not has_column_privilege('authenticated', 'app.shop_integrations', 'refresh_token_ciphertext', 'select'),
  'authenticated cannot read the refresh token');
select ok(not has_column_privilege('authenticated', 'app.shop_integrations', 'wrapped_data_key', 'select'),
  'authenticated cannot read the wrapped data key');
select ok(has_column_privilege('authenticated', 'app.shop_integrations', 'status', 'select'),
  'authenticated may read the status (RLS decides which rows)');
select ok(not has_table_privilege('authenticated', 'app.shop_integrations', 'insert, update, delete'),
  'authenticated cannot write integrations');
select ok(not has_table_privilege('anon', 'app.shop_integrations', 'select'),
  'anon cannot read integrations');
select ok(not has_table_privilege('service_role', 'app.shop_integrations', 'select'),
  'service_role cannot read integrations either');

-- Owner V (no MFA needed to look): their organization's row only, without the sealed columns.
select set_config('request.jwt.claims', '{"sub":"51111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select results_eq(
  $$ select shop_id, status, scopes from app.shop_integrations $$,
  $$ values ('5c000000-0000-0000-0000-000000000001'::uuid, 'connected'::text, '{shops_r}'::text[]) $$,
  'an Owner sees the status and scopes of their own organization only'
);
select throws_ok(
  $$ select access_token_ciphertext from app.shop_integrations $$,
  '42501', null, 'an Owner cannot read the sealed access token'
);
select throws_ok(
  $$ select * from app.shop_integrations $$,
  '42501', null, 'select * fails: the sealed columns are never readable by a user'
);
reset role;

-- Support V has access to the shop, but integrations are for Owner and Admin.
select set_config('request.jwt.claims', '{"sub":"51111111-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is_empty(
  $$ select id from app.shop_integrations $$,
  'a Support member sees no integration'
);
reset role;

-- Constraints.
select throws_ok(
  $$ insert into app.shop_integrations
       (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id,
        wrapped_data_key, access_token_ciphertext, refresh_token_ciphertext, access_expires_at,
        refresh_expires_at, status)
     values (gen_random_uuid(), '5a000000-0000-0000-0000-000000000000',
       '5c000000-0000-0000-0000-000000000001', 'etsy', 101, 201, 'key-a', '\x01aa', '\x01bb',
       '\x01cc', now(), now(), 'connected') $$,
  '23505', null, 'one integration per shop and provider'
);
select throws_ok(
  $$ insert into app.shop_integrations
       (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id,
        wrapped_data_key, access_token_ciphertext, refresh_token_ciphertext, access_expires_at,
        refresh_expires_at, status)
     values (gen_random_uuid(), '5b000000-0000-0000-0000-000000000000',
       '5c000000-0000-0000-0000-000000000001', 'etsy', 101, 201, 'key-a', '\x01aa', '\x01bb',
       '\x01cc', now(), now(), 'connected') $$,
  '23503', null, 'the shop must belong to the same organization'
);
select throws_ok(
  $$ update app.shop_integrations set access_token_ciphertext = convert_to('plain-token', 'UTF8')
     where id = '5c100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a value that is not a sealed envelope is refused'
);
select throws_ok(
  $$ update app.shop_integrations set status = 'disconnected'
     where id = '5c100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a disconnected integration holds no tokens'
);
select lives_ok(
  $$ update app.shop_integrations
     set status = 'disconnected', key_id = null, wrapped_data_key = null,
         access_token_ciphertext = null, refresh_token_ciphertext = null,
         access_expires_at = null, refresh_expires_at = null
     where id = '5c100000-0000-0000-0000-000000000001' $$,
  'disconnecting deletes the tokens and keeps the row'
);
select throws_ok(
  $$ update app.shop_integrations set status = 'connected'
     where id = '5c100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a connected integration must hold its tokens'
);

select * from finish();
rollback;
