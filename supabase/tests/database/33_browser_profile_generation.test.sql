-- The profile archive's generation and the environment's provider (slice 8A.2a, ADR-0025 §1).
-- A new archive starts at generation 1 and every update names exactly the next one, so the
-- adapter's compare-and-set cannot be skipped. Every environment runs `managed-chromium`, the
-- only provider, which is `enabled`; no role can change the provider or the registry
-- (AC-8A.2.6: the Owner's change comes with its endpoint).
begin;
create extension if not exists pgtap with schema extensions;
select plan(17);

insert into app.organizations (id, name) values
  ('b3a00000-0000-0000-0000-000000000000', 'Organization Generation A');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('b3a10000-0000-0000-0000-000000000001', 'b3a00000-0000-0000-0000-000000000000', 'Shop GA', 'etsy', 'active'),
  ('b3a10000-0000-0000-0000-000000000002', 'b3a00000-0000-0000-0000-000000000000', 'Shop GA2', 'etsy', 'active');
insert into app.shop_browser_environments (id, organization_id, shop_id, status) values
  ('b3a20000-0000-0000-0000-000000000001', 'b3a00000-0000-0000-0000-000000000000',
   'b3a10000-0000-0000-0000-000000000001', 'provisioning'),
  ('b3a20000-0000-0000-0000-000000000002', 'b3a00000-0000-0000-0000-000000000000',
   'b3a10000-0000-0000-0000-000000000002', 'provisioning');

-- The generation.
select has_column('app', 'browser_profile_archives', 'generation', 'archives have a generation');
select lives_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b3a20000-0000-0000-0000-000000000001', 'b3a00000-0000-0000-0000-000000000000',
             'b3a10000-0000-0000-0000-000000000001', 'browser-1', '\x01aa', now()) $$,
  'a new archive is written without naming a generation'
);
select is(
  (select generation from app.browser_profile_archives
   where environment_id = 'b3a20000-0000-0000-0000-000000000001'),
  1::bigint, 'a new archive starts at generation 1'
);
select throws_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at, generation)
     values ('b3a20000-0000-0000-0000-000000000002', 'b3a00000-0000-0000-0000-000000000000',
             'b3a10000-0000-0000-0000-000000000002', 'browser-1', '\x01aa', now(), 7) $$,
  '23514', null, 'a new archive cannot start past generation 1'
);
select lives_ok(
  $$ update app.browser_profile_archives set archive = '\x01bb', generation = generation + 1
     where environment_id = 'b3a20000-0000-0000-0000-000000000001' and generation = 1 $$,
  'a write that names the next generation lands'
);
select is(
  (select generation from app.browser_profile_archives
   where environment_id = 'b3a20000-0000-0000-0000-000000000001'),
  2::bigint, 'the write moved the archive to generation 2'
);
select throws_ok(
  $$ update app.browser_profile_archives set archive = '\x01cc'
     where environment_id = 'b3a20000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a write that keeps the generation is refused'
);
select throws_ok(
  $$ update app.browser_profile_archives set archive = '\x01cc', generation = 9
     where environment_id = 'b3a20000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a write that skips generations is refused'
);
select throws_ok(
  $$ update app.browser_profile_archives set archive = '\x01cc', generation = 1
     where environment_id = 'b3a20000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a write that moves a generation back is refused'
);
select is(
  (select count(*) from app.browser_profile_archives
   where environment_id = 'b3a20000-0000-0000-0000-000000000001' and generation = 1),
  0::bigint, 'a stale compare-and-set (generation 1) matches no row'
);
select is(
  (select archive from app.browser_profile_archives
   where environment_id = 'b3a20000-0000-0000-0000-000000000001'),
  '\x01bb'::bytea, 'the refused writes changed nothing'
);

-- The provider.
select is(
  array(select provider from app.shop_browser_environments
        where organization_id = 'b3a00000-0000-0000-0000-000000000000'),
  array['managed-chromium', 'managed-chromium'], 'every environment runs managed-chromium'
);
select is(
  array(select id || ':' || readiness from app.browser_providers order by id),
  array['managed-chromium:enabled'], 'managed-chromium is the only provider, and enabled'
);
select throws_ok(
  $$ update app.shop_browser_environments set provider = 'anti-detect'
     where id = 'b3a20000-0000-0000-0000-000000000001' $$,
  '23503', null, 'an environment can only name a registered provider'
);
select ok(
  not has_column_privilege('authenticated', 'app.shop_browser_environments', 'provider', 'update')
  and not has_column_privilege('htco_browser', 'app.shop_browser_environments', 'provider', 'update')
  and not has_column_privilege('htco_worker', 'app.shop_browser_environments', 'provider', 'update')
  and not has_column_privilege('service_role', 'app.shop_browser_environments', 'provider', 'update'),
  'AC-8A.2.6: no role can change an environment''s provider yet'
);
select ok(
  not has_table_privilege('authenticated', 'app.browser_providers', 'select, insert, update, delete')
  and not has_table_privilege('htco_browser', 'app.browser_providers', 'select, insert, update, delete')
  and not has_table_privilege('htco_worker', 'app.browser_providers', 'select, insert, update, delete')
  and not has_table_privilege('anon', 'app.browser_providers', 'select, insert, update, delete'),
  'no role can read or change the provider registry'
);
select ok((select relrowsecurity from pg_class where oid = 'app.browser_providers'::regclass),
  'the provider registry has RLS enabled');

select * from finish();
rollback;
