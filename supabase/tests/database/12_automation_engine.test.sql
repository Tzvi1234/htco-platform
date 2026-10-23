-- The durable automation engine (slice 5.10): definitions and executions with RLS; one execution
-- per (automation, key); the ExecutionState machine checked by the database; append-only events.
begin;
create extension if not exists pgtap with schema extensions;
select plan(14);

insert into app.organizations (id, name) values
  ('a1000000-0000-0000-0000-000000000000', 'Organization I'),
  ('a2000000-0000-0000-0000-000000000000', 'Organization J');
insert into app.automation_definitions
  (id, version, name, business_owner, primary_trigger, callable_from, retry_max_attempts,
   retry_initial_backoff_seconds, retry_max_backoff_seconds, timeout_seconds, side_effect,
   approval_required, readiness, activation_phase, specification, document, specification_hash,
   registry_commit, synced_at)
values ('pgtap-flow', 1, 'pgTAP flow', 'owner', 'event', '{event}', 2, 1, 10, 30, 'internal',
        false, 'draft', 5, '{}', '{}', repeat('a', 64), 'local', now());
insert into app.automation_executions
  (id, organization_id, automation_id, automation_version, idempotency_key, trigger, actor_kind,
   actor_id, state, correlation_id)
values
  ('a1100000-0000-0000-0000-000000000001', 'a1000000-0000-0000-0000-000000000000', 'pgtap-flow',
   1, 'k-1', 'event', 'system', 'test', 'pending', 'c-1'),
  ('a2100000-0000-0000-0000-000000000001', 'a2000000-0000-0000-0000-000000000000', 'pgtap-flow',
   1, 'k-2', 'event', 'system', 'test', 'pending', 'c-2');
insert into app.automation_execution_events
  (id, organization_id, execution_id, to_state, actor_kind, actor_id, correlation_id, occurred_at)
values ('a1200000-0000-0000-0000-000000000001', 'a1000000-0000-0000-0000-000000000000',
        'a1100000-0000-0000-0000-000000000001', 'pending', 'system', 'test', 'c-1', now());

select ok(
  (select bool_and(relrowsecurity) from pg_class
   where oid in ('app.automation_definitions'::regclass, 'app.automation_executions'::regclass,
                 'app.automation_execution_events'::regclass)),
  'the engine tables have RLS enabled'
);
select ok(not has_table_privilege('authenticated', 'app.automation_executions', 'insert, update, delete'),
  'users cannot write executions');
select ok(not has_table_privilege('authenticated', 'app.automation_definitions', 'select'),
  'users read definitions only through the API');
select ok(not has_table_privilege('anon', 'app.automation_executions', 'select'),
  'anon reads no execution');
select ok(not has_table_privilege('htco_worker', 'app.automation_definitions', 'insert, update, delete'),
  'the worker cannot change definitions');
select throws_ok(
  $$ insert into app.automation_executions
       (id, organization_id, automation_id, automation_version, idempotency_key, trigger,
        actor_kind, actor_id, state, correlation_id)
     values ('a1100000-0000-0000-0000-000000000002', 'a1000000-0000-0000-0000-000000000000',
             'pgtap-flow', 1, 'k-1', 'event', 'system', 'test', 'pending', 'c-1') $$,
  '23505', null, 'AC-5.10.1: one execution per automation and idempotency key'
);
select throws_ok(
  $$ update app.automation_executions set state = 'succeeded'
     where id = 'a1100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'AC-5.10.6: pending -> succeeded is not in the machine'
);
select lives_ok(
  $$ update app.automation_executions set state = 'running', attempts = 1
     where id = 'a1100000-0000-0000-0000-000000000001' $$,
  'pending -> running is'
);
select lives_ok(
  $$ update app.automation_executions set state = 'waiting'
     where id = 'a1100000-0000-0000-0000-000000000001' $$,
  'running -> waiting (an action waits for an approval or a person)'
);
select throws_ok(
  $$ update app.automation_executions set state = 'succeeded'
     where id = 'a1100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'waiting -> succeeded is not in the machine'
);
select throws_ok(
  $$ delete from app.automation_execution_events
     where id = 'a1200000-0000-0000-0000-000000000001' $$,
  '42501', null, 'state changes are append-only'
);
select throws_ok(
  $$ insert into app.automation_executions
       (id, organization_id, automation_id, automation_version, idempotency_key, trigger,
        actor_kind, actor_id, state, correlation_id)
     values ('a1100000-0000-0000-0000-000000000003', 'a1000000-0000-0000-0000-000000000000',
             'no-such-flow', 1, 'k-3', 'event', 'system', 'test', 'pending', 'c-3') $$,
  '23503', null, 'an execution names a stored definition version'
);

grant usage on schema extensions to htco_worker;
set local role htco_worker;
select set_config('htco.organization_id', 'a1000000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select id from app.automation_executions $$,
  $$ values ('a1100000-0000-0000-0000-000000000001'::uuid) $$,
  'the worker sees its job''s organization''s executions only'
);
select is(
  (select count(*)::int from app.automation_definitions where id = 'pgtap-flow'), 1,
  'the worker reads definitions'
);
reset role;

select * from finish();
rollback;
