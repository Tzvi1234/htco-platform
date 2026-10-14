-- The HTCO schemas exist and are closed by default (AC-2.1.1, ER-SEC-02); slice 2.2 opens
-- `app` and `audit` to `authenticated` only, with RLS on every table.
begin;
create extension if not exists pgtap with schema extensions;

select plan(10);

select has_schema('app');
select has_schema('private');
select has_schema('audit');

select ok(
  not has_schema_privilege('anon', 'app', 'usage'),
  'anon cannot use the app schema'
);
select ok(
  has_schema_privilege('authenticated', 'app', 'usage'),
  'authenticated uses the app schema (granted in slice 2.2; RLS decides which rows)'
);
select ok(
  not has_schema_privilege('anon', 'private', 'usage'),
  'anon cannot use the private schema'
);
select ok(
  not has_schema_privilege('anon', 'private', 'usage')
    and not has_schema_privilege('service_role', 'private', 'usage'),
  'only authenticated may call the private policy helpers'
);
select ok(
  not has_schema_privilege('anon', 'audit', 'usage'),
  'anon cannot use the audit schema'
);
select ok(
  not has_table_privilege('authenticated', 'audit.audit_log', 'update')
    and not has_table_privilege('authenticated', 'audit.audit_log', 'delete'),
  'authenticated can never update or delete audit records'
);
select is(
  (select count(*)::int from pg_tables where schemaname = 'public'),
  0,
  'no business table is created in the exposed public schema'
);

select * from finish();
rollback;
