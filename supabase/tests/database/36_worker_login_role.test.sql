-- The worker's own login (architecture audit 5.1, ER-SEC-08): `htco_worker_login` can log in,
-- holds no right of its own, is no member of the owner or of a client role, and can only switch
-- to `htco_worker`.
begin;
create extension if not exists pgtap with schema extensions;
select plan(10);

select ok(
  (select rolcanlogin from pg_roles where rolname = 'htco_worker_login'),
  'htco_worker_login exists and is a login'
);
select ok(
  (select not rolsuper and not rolbypassrls and not rolinherit and not rolcreaterole
          and not rolcreatedb and not rolreplication
   from pg_roles where rolname = 'htco_worker_login'),
  'it is no superuser, does not bypass RLS, does not inherit and cannot create roles or databases'
);
select ok(pg_has_role('htco_worker_login', 'htco_worker', 'member'),
  'it is a member of the worker role');
select ok(pg_has_role('htco_worker_login', 'htco_worker', 'set'),
  'it may switch to the worker role');
select ok(not pg_has_role('htco_worker_login', 'htco_worker', 'usage'),
  'it does not inherit the worker role''s rights');
select is(
  array(
    select r.rolname::text from pg_roles r
    where r.rolname in ('postgres', 'authenticated', 'anon', 'service_role', 'htco_browser',
                        'supabase_admin', 'authenticator')
      and pg_has_role('htco_worker_login', r.oid, 'member')
    order by 1
  ),
  array[]::text[],
  'it is no member of the owner, the client roles or the browser worker'
);
select ok(
  not has_table_privilege('htco_worker_login', 'app.shop_integrations', 'select, insert, update, delete')
  and not has_any_column_privilege('htco_worker_login', 'app.shop_integrations', 'select'),
  'before switching, it cannot read the token vault'
);
select is(
  array(
    select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where c.relkind in ('r', 'v', 'm', 'p', 'f') and n.nspname in ('app', 'audit', 'jobs', 'private', 'auth', 'storage')
      and has_table_privilege('htco_worker_login', c.oid, 'select, insert, update, delete, truncate, references, trigger')
    order by 1
  ),
  array[]::text[],
  'before switching, it holds no table right in the application schemas'
);

-- A `reset role` lands on the login, which reaches nothing.
set local role htco_worker_login;
select throws_ok($$ select count(*) from jobs.jobs $$, '42501', null,
  'as the login itself, the queue is out of reach');
select throws_ok($$ select count(*) from app.shop_integrations $$, '42501', null,
  'as the login itself, the token vault is out of reach');
reset role;

select * from finish();
rollback;
