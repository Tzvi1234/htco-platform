-- Base schemas exist, and client roles get no more than usage (slice 2.1).
begin;
create extension if not exists pgtap with schema extensions;
select plan(8);

select has_schema('app');
select has_schema('private');
select has_schema('audit');

select ok(
  not has_schema_privilege('anon', 'app', 'usage'),
  'anon cannot use the app schema'
);
select ok(
  not has_schema_privilege('authenticated', 'private', 'usage'),
  'authenticated cannot use the private schema'
);
select ok(
  not has_schema_privilege('anon', 'private', 'usage'),
  'anon cannot use the private schema'
);
select ok(
  not has_schema_privilege('authenticated', 'app', 'create'),
  'authenticated cannot create objects in app'
);
select ok(
  not has_schema_privilege('authenticated', 'audit', 'create'),
  'authenticated cannot create objects in audit'
);

select * from finish();
rollback;
