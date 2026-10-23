-- The Action Gateway's executions (slice 5.12): one row per organization, action and key; finished
-- rows never change; users read their organization's or granted shops' rows; the worker its
-- job's organization only.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(9);

insert into app.organizations (id, name) values
  ('b1000000-0000-0000-0000-000000000000', 'Organization K'),
  ('b2000000-0000-0000-0000-000000000000', 'Organization L');
insert into app.action_executions
  (id, organization_id, action_name, action_version, idempotency_key, request_hash, state,
   outcome, actor_kind, actor_id, correlation_id)
values
  ('b1100000-0000-0000-0000-000000000001', 'b1000000-0000-0000-0000-000000000000',
   'etsy.receipts.read', 1, 'k-1', repeat('a', 64), 'succeeded', 'succeeded', 'automation',
   'etsy-order-intake', 'c-1'),
  ('b2100000-0000-0000-0000-000000000001', 'b2000000-0000-0000-0000-000000000000',
   'etsy.receipts.read', 1, 'k-1', repeat('a', 64), 'received', null, 'automation',
   'etsy-order-intake', 'c-2');

select ok((select relrowsecurity from pg_class where oid = 'app.action_executions'::regclass),
  'action_executions has RLS enabled');
select ok(not has_table_privilege('authenticated', 'app.action_executions', 'insert, update, delete'),
  'users cannot write action executions');
select ok(not has_column_privilege('authenticated', 'app.action_executions', 'output', 'select'),
  'users never read stored outputs directly');
select ok(not has_table_privilege('anon', 'app.action_executions', 'select'),
  'anon reads nothing');
select throws_ok(
  $$ insert into app.action_executions
       (id, organization_id, action_name, action_version, idempotency_key, request_hash, state,
        actor_kind, actor_id, correlation_id)
     values ('b1100000-0000-0000-0000-000000000002', 'b1000000-0000-0000-0000-000000000000',
             'etsy.receipts.read', 1, 'k-1', repeat('a', 64), 'received', 'system', 'x', 'c') $$,
  '23505', null, 'AC-5.12.1: one execution per organization, action and key'
);
select throws_ok(
  $$ update app.action_executions set state = 'running'
     where id = 'b1100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a finished execution never changes'
);
select throws_ok(
  $$ insert into app.action_executions
       (id, organization_id, action_name, action_version, idempotency_key, request_hash, state,
        actor_kind, actor_id, correlation_id)
     values ('b1100000-0000-0000-0000-000000000003', 'b1000000-0000-0000-0000-000000000000',
             'etsy.receipts.read', 1, 'k-3', 'not-a-hash', 'received', 'system', 'x', 'c') $$,
  '23514', null, 'the request hash is a SHA-256'
);

set local role htco_worker;
select set_config('htco.organization_id', 'b1000000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select id from app.action_executions where action_name = 'etsy.receipts.read'
     and id in ('b1100000-0000-0000-0000-000000000001', 'b2100000-0000-0000-0000-000000000001') $$,
  $$ values ('b1100000-0000-0000-0000-000000000001'::uuid) $$,
  'the worker sees its job''s organization''s executions only'
);
select is_empty(
  $$ update app.action_executions set state = 'routed'
     where organization_id = 'b2000000-0000-0000-0000-000000000000' returning id $$,
  'the worker cannot change another organization''s rows'
);
reset role;

select * from finish();
rollback;
