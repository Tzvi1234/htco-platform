-- The browser worker's role and the sealed profile archives (slice 8A.1): `htco_browser` reaches
-- its organization's environments and archives only, never `shop_integrations`, tokens or
-- another organization's environments (AC-8A.1.2); no client role sees the archives; an archive
-- is bound to its environment, shop and organization, and capped at 25 MiB.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_browser;
select plan(26);

insert into app.organizations (id, name) values
  ('b8a00000-0000-0000-0000-000000000000', 'Organization Browser A'),
  ('b8b00000-0000-0000-0000-000000000000', 'Organization Browser B');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('b8a10000-0000-0000-0000-000000000001', 'b8a00000-0000-0000-0000-000000000000', 'Shop BA', 'etsy', 'active'),
  ('b8a10000-0000-0000-0000-000000000002', 'b8a00000-0000-0000-0000-000000000000', 'Shop BA2', 'etsy', 'active'),
  ('b8b10000-0000-0000-0000-000000000001', 'b8b00000-0000-0000-0000-000000000000', 'Shop BB', 'etsy', 'active');
insert into app.shop_browser_environments (id, organization_id, shop_id, status) values
  ('b8a20000-0000-0000-0000-000000000001', 'b8a00000-0000-0000-0000-000000000000',
   'b8a10000-0000-0000-0000-000000000001', 'provisioning'),
  ('b8a20000-0000-0000-0000-000000000002', 'b8a00000-0000-0000-0000-000000000000',
   'b8a10000-0000-0000-0000-000000000002', 'provisioning'),
  ('b8b20000-0000-0000-0000-000000000001', 'b8b00000-0000-0000-0000-000000000000',
   'b8b10000-0000-0000-0000-000000000001', 'provisioning');
insert into app.browser_profile_archives
  (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
values
  ('b8b20000-0000-0000-0000-000000000001', 'b8b00000-0000-0000-0000-000000000000',
   'b8b10000-0000-0000-0000-000000000001', 'browser-1', '\x01aabbcc', now());

-- The role.
select ok(
  (select not rolcanlogin and not rolsuper and not rolbypassrls and not rolinherit
   from pg_roles where rolname = 'htco_browser'),
  'htco_browser cannot log in, is no superuser, does not inherit and does not bypass RLS'
);
select ok(not pg_has_role('htco_browser', 'htco_worker', 'member'),
  'the browser role is not the worker role');

-- AC-8A.1.2: no shop integrations, tokens or other secrets.
select ok(not has_table_privilege('htco_browser', 'app.shop_integrations', 'select, insert, update, delete'),
  'AC-8A.1.2: the browser role has no right on shop_integrations');
select ok(not has_any_column_privilege('htco_browser', 'app.shop_integrations', 'select'),
  'AC-8A.1.2: nor on any of its columns');
select ok(not has_schema_privilege('htco_browser', 'jobs', 'usage'),
  'the browser role cannot reach the jobs schema: its status event goes through app.browser_publish_status_change (sage on #170)');
select ok(not has_schema_privilege('htco_browser', 'private', 'usage'),
  'the browser role cannot reach the private schema');
select is(
  array(
    select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where c.relkind in ('r', 'v', 'm', 'p', 'f') and n.nspname in ('app', 'audit', 'jobs', 'private', 'auth', 'storage')
      and has_table_privilege('htco_browser', c.oid, 'select, insert, update, delete, truncate, references, trigger')
    order by 1
  ),
  array['app.browser_environment_events', 'app.browser_profile_archives', 'app.browser_sessions',
        'audit.audit_log'],
  'AC-8A.1.2: the browser role holds table rights on its archives, its sessions (8A.2), the history (8A.3) and the audit log only'
);
select is(
  array(
    select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where c.relkind in ('r', 'v', 'm', 'p', 'f') and n.nspname in ('app', 'audit', 'jobs', 'private', 'auth', 'storage')
      and has_any_column_privilege('htco_browser', c.oid, 'select, insert, update, references')
    order by 1
  ),
  array['app.browser_environment_events', 'app.browser_profile_archives', 'app.browser_sessions',
        'app.shop_browser_environments', 'audit.audit_log'],
  'AC-8A.1.2: beyond those, its only column rights are on the browser environments'
);
select ok(
  not has_column_privilege('htco_browser', 'app.shop_browser_environments', 'consent_ip_hash', 'select')
  and not has_column_privilege('htco_browser', 'app.shop_browser_environments', 'consent_user_id', 'select'),
  'the browser role never reads the consent record'
);
select ok(not has_column_privilege('htco_browser', 'app.shop_browser_environments', 'consent_at', 'update')
  and not has_column_privilege('htco_browser', 'app.shop_browser_environments', 'consent_ip_hash', 'update'),
  'the browser role never writes the consent record (its status edges are tested in 31)');
select results_eq(
  $$ select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
     where p.prosecdef and n.nspname in ('app', 'audit', 'public')
       and has_function_privilege('htco_browser', p.oid, 'execute') order by 1 $$,
  $$ values ('app.browser_publish_status_change(uuid)') $$,
  'the only security-definer function the browser role can call is its status-event writer (sage on #170)'
);

-- No client role sees the archives.
select ok((select relrowsecurity from pg_class where oid = 'app.browser_profile_archives'::regclass),
  'browser_profile_archives has RLS enabled');
select ok(
  not has_table_privilege('anon', 'app.browser_profile_archives', 'select, insert, update, delete')
  and not has_table_privilege('authenticated', 'app.browser_profile_archives', 'select, insert, update, delete')
  and not has_table_privilege('service_role', 'app.browser_profile_archives', 'select, insert, update, delete'),
  'anon, authenticated and service_role have no right on the archives'
);
select ok(not has_table_privilege('htco_worker', 'app.browser_profile_archives', 'select'),
  'the agent-worker never reads a profile (ADR-0020 §8)');

-- Constraints: bound to the environment, shop and organization; capped; sealed bytes only.
select throws_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b8a20000-0000-0000-0000-000000000001', 'b8a00000-0000-0000-0000-000000000000',
             'b8a10000-0000-0000-0000-000000000002', 'browser-1', '\x01', now()) $$,
  '23503', null, 'an archive cannot name another shop than its environment''s'
);
select throws_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b8a20000-0000-0000-0000-000000000001', 'b8a00000-0000-0000-0000-000000000000',
             'b8a10000-0000-0000-0000-000000000001', 'browser-1',
             decode(repeat('01', 26214401), 'hex'),
             now()) $$,
  '23514', null, 'an archive over 25 MiB is refused'
);
select lives_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b8a20000-0000-0000-0000-000000000002', 'b8a00000-0000-0000-0000-000000000000',
             'b8a10000-0000-0000-0000-000000000002', 'browser-1',
             decode(repeat('01', 26214400), 'hex'), now()) $$,
  'an archive of exactly 25 MiB is kept'
);
delete from app.browser_profile_archives
  where environment_id = 'b8a20000-0000-0000-0000-000000000002';
select throws_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b8a20000-0000-0000-0000-000000000002', 'b8a00000-0000-0000-0000-000000000000',
             'b8a10000-0000-0000-0000-000000000002', 'browser-1', '', now()) $$,
  '23514', null, 'an empty archive is refused'
);
select throws_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b8a20000-0000-0000-0000-000000000002', 'b8a00000-0000-0000-0000-000000000000',
             'b8a10000-0000-0000-0000-000000000002', 'key id; drop', '\x01', now()) $$,
  '23514', null, 'a key ID is a short name, never free text'
);

-- AC-8A.1.2: one organization at a time.
set local role htco_browser;
select is_empty(
  $$ select id from app.shop_browser_environments $$,
  'without an organization the browser role sees no environment'
);
select set_config('htco.organization_id', 'b8a00000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select id from app.shop_browser_environments order by id $$,
  $$ values ('b8a20000-0000-0000-0000-000000000001'::uuid), ('b8a20000-0000-0000-0000-000000000002'::uuid) $$,
  'AC-8A.1.2: the browser role sees its organization''s environments only'
);
select is_empty(
  $$ select environment_id from app.browser_profile_archives $$,
  'AC-8A.1.2: and never another organization''s archive'
);
select throws_ok(
  $$ select * from app.shop_integrations $$,
  '42501', null, 'AC-8A.1.2: reading shop_integrations is refused'
);
select lives_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b8a20000-0000-0000-0000-000000000001', 'b8a00000-0000-0000-0000-000000000000',
             'b8a10000-0000-0000-0000-000000000001', 'browser-1', '\x01aa', now());
     update app.shop_browser_environments
       set profile_storage_key = 'browser-profiles/b8a00000-0000-0000-0000-000000000000/b8a10000-0000-0000-0000-000000000001/b8a20000-0000-0000-0000-000000000001',
           profile_key_id = 'browser-1'
       where id = 'b8a20000-0000-0000-0000-000000000001' $$,
  'the browser role stores its own archive and records it on the environment'
);
select throws_ok(
  $$ insert into app.browser_profile_archives
       (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
     values ('b8b20000-0000-0000-0000-000000000001', 'b8b00000-0000-0000-0000-000000000000',
             'b8b10000-0000-0000-0000-000000000001', 'browser-1', '\x01', now())
     on conflict (environment_id) do update set archive = excluded.archive $$,
  '42501', null, 'AC-8A.1.2: it cannot write another organization''s archive'
);
update app.shop_browser_environments set profile_key_id = 'stolen'
  where id = 'b8b20000-0000-0000-0000-000000000001';
reset role;
select is(
  (select profile_key_id from app.shop_browser_environments
   where id = 'b8b20000-0000-0000-0000-000000000001'),
  null, 'AC-8A.1.2: nor change another organization''s environment'
);

select * from finish();
rollback;
