-- Browser Configuration Profiles (slice 8A.2b, AC-8A.2.10): append-only versions, each the next
-- number for its environment; the locale is one of the shop's recorded languages; honest by
-- design (no fingerprint columns); bound to its environment; the Owner and Admins read their
-- organization's versions, and no other role has any right on the table.
begin;
create extension if not exists pgtap with schema extensions;
select plan(23);

insert into auth.users (id, email) values
  ('c3a00000-0000-0000-0000-0000000000a1', 'owner-bc@example.test'),
  ('c3a00000-0000-0000-0000-0000000000a2', 'operations-bc@example.test'),
  ('c3b00000-0000-0000-0000-0000000000a1', 'owner-bc-b@example.test');
insert into app.organizations (id, name) values
  ('c3a00000-0000-0000-0000-000000000000', 'Organization Configuration A'),
  ('c3b00000-0000-0000-0000-000000000000', 'Organization Configuration B');
insert into app.memberships (id, organization_id, user_id, role) values
  ('c3a00000-0000-0000-0000-0000000000b1', 'c3a00000-0000-0000-0000-000000000000', 'c3a00000-0000-0000-0000-0000000000a1', 'owner'),
  ('c3a00000-0000-0000-0000-0000000000b2', 'c3a00000-0000-0000-0000-000000000000', 'c3a00000-0000-0000-0000-0000000000a2', 'operations'),
  ('c3b00000-0000-0000-0000-0000000000b1', 'c3b00000-0000-0000-0000-000000000000', 'c3b00000-0000-0000-0000-0000000000a1', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('c3a10000-0000-0000-0000-000000000001', 'c3a00000-0000-0000-0000-000000000000', 'Shop CA', 'etsy', 'active'),
  ('c3a10000-0000-0000-0000-000000000002', 'c3a00000-0000-0000-0000-000000000000', 'Shop CA2', 'etsy', 'active'),
  ('c3b10000-0000-0000-0000-000000000001', 'c3b00000-0000-0000-0000-000000000000', 'Shop CB', 'etsy', 'active');
insert into app.shop_browser_environments (id, organization_id, shop_id, status) values
  ('c3a20000-0000-0000-0000-000000000001', 'c3a00000-0000-0000-0000-000000000000',
   'c3a10000-0000-0000-0000-000000000001', 'provisioning'),
  ('c3a20000-0000-0000-0000-000000000002', 'c3a00000-0000-0000-0000-000000000000',
   'c3a10000-0000-0000-0000-000000000002', 'provisioning'),
  ('c3b20000-0000-0000-0000-000000000001', 'c3b00000-0000-0000-0000-000000000000',
   'c3b10000-0000-0000-0000-000000000001', 'provisioning');

-- Structure and grants.
select ok((select relrowsecurity from pg_class where oid = 'app.browser_configuration_profiles'::regclass),
  'browser_configuration_profiles has RLS enabled');
select ok(
  not exists (
    select from information_schema.columns
    where table_schema = 'app' and table_name = 'browser_configuration_profiles'
      and column_name ~ '(user_agent|screen|resolution|viewport|time_?zone|geo|latitude|longitude|fingerprint|stealth|extension|proxy|switch|args)'
  ),
  'AC-8A.2.10: no column can hold a never-configurable setting'
);
select ok(
  not has_table_privilege('anon', 'app.browser_configuration_profiles', 'select, insert, update, delete')
  and not has_table_privilege('authenticated', 'app.browser_configuration_profiles', 'insert, update, delete'),
  'no client role writes a configuration'
);
select ok(
  not has_table_privilege('htco_browser', 'app.browser_configuration_profiles', 'select, insert, update, delete')
  and not has_table_privilege('htco_worker', 'app.browser_configuration_profiles', 'select, insert, update, delete')
  and not has_table_privilege('service_role', 'app.browser_configuration_profiles', 'select, insert, update, delete'),
  'neither worker role nor service_role has any right on the table'
);

-- Versions: 1, then exactly the next one.
select lives_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages, egress_id,
        actor_kind, actor_id, correlation_id, created_at)
     values ('c3a30000-0000-0000-0000-000000000001', 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             1, 'he-IL', array['he-IL', 'en-US'], 'shared', 'user',
             'c3a00000-0000-0000-0000-0000000000a1', 'req-1', now()) $$,
  'the first version is 1'
);
select lives_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values ('c3a30000-0000-0000-0000-000000000002', 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             2, 'en-US', array['he-IL', 'en-US'], 'user',
             'c3a00000-0000-0000-0000-0000000000a1', 'req-2', now()) $$,
  'the next version is 2, and the egress defaults to shared'
);
select is(
  (select egress_id from app.browser_configuration_profiles where id = 'c3a30000-0000-0000-0000-000000000002'),
  'shared', 'a version without an egress uses the shared one'
);
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             4, 'en-US', array['en-US'], 'user', 'u', 'req-3', now()) $$,
  '40001', null, 'a version that skips a number is refused'
);
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             2, 'en-US', array['en-US'], 'user', 'u', 'req-3', now()) $$,
  '40001', null, 'a version already taken is refused'
);
select lives_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000002', 'c3a20000-0000-0000-0000-000000000002',
             1, 'en-US', array['en-US'], 'user', 'u', 'req-4', now()) $$,
  'another environment counts its own versions'
);

-- Append-only.
select throws_ok(
  $$ update app.browser_configuration_profiles set locale = 'he-IL'
     where id = 'c3a30000-0000-0000-0000-000000000002' $$,
  '42501', null, 'a version cannot be changed'
);
select throws_ok(
  $$ delete from app.browser_configuration_profiles where id = 'c3a30000-0000-0000-0000-000000000001' $$,
  '42501', null, 'a version cannot be deleted'
);

-- Shape: AC-8A.2.10.
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             3, 'fr-FR', array['he-IL', 'en-US'], 'user', 'u', 'req-5', now()) $$,
  '23514', null, 'AC-8A.2.10: a locale outside the shop''s recorded languages is refused'
);
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             3, 'en_US', array['en_US'], 'user', 'u', 'req-5', now()) $$,
  '23514', null, 'a locale is a language tag'
);
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages, egress_id,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             3, 'en-US', array['en-US'], 'http://user:pw@proxy.example:8080', 'user', 'u', 'req-5', now()) $$,
  '23514', null, 'an egress is an identifier, never a proxy address'
);
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3a10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             3, 'en-US', array['en-US'], 'automation', 'nightly', 'req-5', now()) $$,
  '23514', null, 'AC-8A.2.10: only a person saves a configuration, never an agent or automation'
);
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3a00000-0000-0000-0000-000000000000',
             'c3b10000-0000-0000-0000-000000000001', 'c3a20000-0000-0000-0000-000000000001',
             3, 'en-US', array['en-US'], 'user', 'u', 'req-5', now()) $$,
  '23503', null, 'a version cannot name another shop than its environment''s'
);

-- The Owner reads the organization's versions; Operations and another organization do not.
insert into app.browser_configuration_profiles
  (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
   actor_kind, actor_id, correlation_id, created_at)
values ('c3b30000-0000-0000-0000-000000000001', 'c3b00000-0000-0000-0000-000000000000',
        'c3b10000-0000-0000-0000-000000000001', 'c3b20000-0000-0000-0000-000000000001',
        1, 'en-US', array['en-US'], 'user', 'u', 'req-6', now());
set local role authenticated;
select set_config('request.jwt.claims',
  '{"sub": "c3a00000-0000-0000-0000-0000000000a1", "role": "authenticated"}', true);
select is((select count(*)::int from app.browser_configuration_profiles), 3,
  'the Owner reads the organization''s versions only');
select set_config('request.jwt.claims',
  '{"sub": "c3a00000-0000-0000-0000-0000000000a2", "role": "authenticated"}', true);
select is((select count(*)::int from app.browser_configuration_profiles), 0,
  'Operations reads none');
select set_config('request.jwt.claims',
  '{"sub": "c3b00000-0000-0000-0000-0000000000a1", "role": "authenticated"}', true);
select results_eq(
  $$ select id from app.browser_configuration_profiles $$,
  $$ values ('c3b30000-0000-0000-0000-000000000001'::uuid) $$,
  'another organization''s Owner reads only their own'
);
select throws_ok(
  $$ insert into app.browser_configuration_profiles
       (id, organization_id, shop_id, environment_id, version, locale, shop_languages,
        actor_kind, actor_id, correlation_id, created_at)
     values (gen_random_uuid(), 'c3b00000-0000-0000-0000-000000000000',
             'c3b10000-0000-0000-0000-000000000001', 'c3b20000-0000-0000-0000-000000000001',
             2, 'en-US', array['en-US'], 'user', 'u', 'req-7', now()) $$,
  '42501', null, 'the Owner cannot write a version around the API'
);
reset role;

-- Deleting the environment takes its versions with it.
select lives_ok(
  $$ delete from app.shop_browser_environments where id = 'c3a20000-0000-0000-0000-000000000002' $$,
  'an environment with versions can still be deleted'
);
select is(
  (select count(*)::int from app.browser_configuration_profiles
   where environment_id = 'c3a20000-0000-0000-0000-000000000002'),
  0, 'its versions went with it'
);

select * from finish();
rollback;
