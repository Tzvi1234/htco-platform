-- The HTCO schemas exist and are closed by default (AC-2.1.1, ER-SEC-02).
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
  not has_schema_privilege('authenticated', 'app', 'usage'),
  'authenticated cannot use the app schema until a slice grants it'
);
select ok(
  not has_schema_privilege('anon', 'private', 'usage'),
  'anon cannot use the private schema'
);
select ok(
  not has_schema_privilege('authenticated', 'private', 'usage'),
  'authenticated cannot use the private schema'
);
select ok(
  not has_schema_privilege('anon', 'audit', 'usage'),
  'anon cannot use the audit schema'
);
select ok(
  not has_schema_privilege('authenticated', 'audit', 'usage'),
  'authenticated cannot use the audit schema directly'
);
select is(
  (select count(*)::int from pg_tables where schemaname = 'public'),
  0,
  'no business table is created in the exposed public schema'
);

select * from finish();
rollback;
