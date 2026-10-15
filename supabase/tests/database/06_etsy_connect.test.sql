-- Etsy connect (slice 4.3): pending OAuth authorizations are server-only, short-lived, sealed and
-- bound to a web session.
begin;
create extension if not exists pgtap with schema extensions;
select plan(13);

insert into auth.users (id, email) values
  ('61111111-0000-0000-0000-000000000001', 'owner-x@example.test');
insert into app.organizations (id, name) values
  ('6a000000-0000-0000-0000-000000000000', 'Organization X');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('6c000000-0000-0000-0000-000000000001', '6a000000-0000-0000-0000-000000000000', 'Shop X', 'etsy', 'active'),
  ('6c000000-0000-0000-0000-000000000002', '6a000000-0000-0000-0000-000000000000', 'Shop X2', 'etsy', 'active');
insert into private.web_sessions
  (id, token_hash, user_id, access_token, refresh_token, access_expires_at, idle_expires_at,
   absolute_expires_at)
values
  ('6e000000-0000-0000-0000-000000000001', '\x6e01', '61111111-0000-0000-0000-000000000001',
   '\x01', '\x01', now(), now() + interval '1 hour', now() + interval '1 day');
insert into app.shop_integrations
  (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id, wrapped_data_key,
   access_token_ciphertext, refresh_token_ciphertext, scopes, access_expires_at,
   refresh_expires_at, status)
values
  ('6c100000-0000-0000-0000-000000000001', '6a000000-0000-0000-0000-000000000000',
   '6c000000-0000-0000-0000-000000000001', 'etsy', 601, 701, 'key-a', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected');

select has_table('private', 'oauth_authorizations', 'private.oauth_authorizations exists');
select ok(
  (select relrowsecurity from pg_class where oid = 'private.oauth_authorizations'::regclass),
  'oauth_authorizations has RLS enabled (and no policy: no client role reads it)'
);
select ok(not has_table_privilege('authenticated', 'private.oauth_authorizations', 'select'),
  'authenticated cannot read pending authorizations');
select ok(not has_table_privilege('anon', 'private.oauth_authorizations', 'select'),
  'anon cannot read pending authorizations');
select ok(not has_table_privilege('service_role', 'private.oauth_authorizations', 'select'),
  'service_role cannot read pending authorizations either');
select col_type_is('private', 'oauth_authorizations', 'code_verifier', 'bytea',
  'the PKCE verifier is stored sealed (bytes), never as text');
select col_is_unique('private', 'oauth_authorizations', 'state_hash',
  'one pending authorization per state');

select lives_ok(
  $$ insert into private.oauth_authorizations
       (id, state_hash, provider, organization_id, user_id, web_session_id, code_verifier, scopes,
        redirect_uri, created_at, expires_at)
     values (gen_random_uuid(), sha256('state-1'), 'etsy', '6a000000-0000-0000-0000-000000000000',
             '61111111-0000-0000-0000-000000000001', '6e000000-0000-0000-0000-000000000001',
             '\x01ff', '{shops_r}', 'https://dash.ht-co.app/integrations/etsy/callback', now(),
             now() + interval '10 minutes') $$,
  'a sealed, ten-minute authorization bound to a web session is accepted'
);
select throws_ok(
  $$ insert into private.oauth_authorizations
       (id, state_hash, provider, organization_id, user_id, web_session_id, code_verifier, scopes,
        redirect_uri, created_at, expires_at)
     values (gen_random_uuid(), sha256('state-2'), 'etsy', '6a000000-0000-0000-0000-000000000000',
             '61111111-0000-0000-0000-000000000001', '6e000000-0000-0000-0000-000000000001',
             '\x01ff', '{shops_r}', 'https://dash.ht-co.app/integrations/etsy/callback', now(),
             now() + interval '11 minutes') $$,
  '23514', null, 'an authorization lives at most ten minutes'
);
select throws_ok(
  $$ insert into private.oauth_authorizations
       (id, state_hash, provider, organization_id, user_id, web_session_id, code_verifier, scopes,
        redirect_uri, created_at, expires_at)
     values (gen_random_uuid(), sha256('state-3'), 'etsy', '6a000000-0000-0000-0000-000000000000',
             '61111111-0000-0000-0000-000000000001', '6e000000-0000-0000-0000-000000000001',
             'plain-verifier', '{shops_r}', 'https://dash.ht-co.app/integrations/etsy/callback',
             now(), now() + interval '10 minutes') $$,
  '23514', null, 'a plaintext verifier is refused'
);
select throws_ok(
  $$ insert into private.oauth_authorizations
       (id, state_hash, provider, organization_id, user_id, web_session_id, code_verifier, scopes,
        redirect_uri, created_at, expires_at)
     values (gen_random_uuid(), '\x01', 'etsy', '6a000000-0000-0000-0000-000000000000',
             '61111111-0000-0000-0000-000000000001', '6e000000-0000-0000-0000-000000000001',
             '\x01ff', '{shops_r}', 'https://dash.ht-co.app/integrations/etsy/callback', now(),
             now() + interval '10 minutes') $$,
  '23514', null, 'the state is stored as a SHA-256 hash (32 bytes)'
);
select throws_ok(
  $$ insert into private.oauth_authorizations
       (id, state_hash, provider, organization_id, user_id, web_session_id, code_verifier, scopes,
        redirect_uri, created_at, expires_at)
     values (gen_random_uuid(), sha256('state-4'), 'etsy', '6a000000-0000-0000-0000-000000000000',
             '61111111-0000-0000-0000-000000000001', gen_random_uuid(), '\x01ff', '{shops_r}',
             'https://dash.ht-co.app/integrations/etsy/callback', now(),
             now() + interval '10 minutes') $$,
  '23503', null, 'an authorization belongs to a real web session'
);

delete from private.web_sessions where id = '6e000000-0000-0000-0000-000000000001';
select is(
  (select count(*) from private.oauth_authorizations
   where user_id = '61111111-0000-0000-0000-000000000001')::int,
  0,
  'ending (deleting) the web session deletes its pending authorizations'
);

select * from finish();
rollback;
