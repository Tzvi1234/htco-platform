-- A waiting execution names the action it waits for (AUD-03, Codex on #175): only a `waiting`
-- row has one, the worker writes it for its job's organization only, and a signed-in user does
-- not read it directly.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(7);

insert into app.organizations (id, name) values
  ('33a00000-0000-0000-0000-000000000000', 'Organization Wait'),
  ('33b00000-0000-0000-0000-000000000000', 'Organization Other');
insert into app.automation_definitions
  (id, version, name, business_owner, primary_trigger, callable_from, retry_max_attempts,
   retry_initial_backoff_seconds, retry_max_backoff_seconds, timeout_seconds, side_effect,
   approval_required, readiness, activation_phase, specification, document, specification_hash,
   registry_commit, synced_at)
values ('pgtap-wait', 1, 'pgTAP wait', 'owner', 'schedule', '{schedule}', 2, 1, 10, 30,
        'internal', false, 'draft', 5, '{}', '{}', repeat('a', 64), 'local', now());
insert into app.automation_executions
  (id, organization_id, automation_id, automation_version, idempotency_key, trigger, actor_kind,
   actor_id, state, correlation_id)
values
  ('33c00000-0000-0000-0000-000000000001', '33a00000-0000-0000-0000-000000000000',
   'pgtap-wait', 1, 'wait-1', 'schedule', 'system', 'scheduler', 'running', 'c-1'),
  ('33c00000-0000-0000-0000-000000000002', '33b00000-0000-0000-0000-000000000000',
   'pgtap-wait', 1, 'wait-2', 'schedule', 'system', 'scheduler', 'running', 'c-2');

select has_column('app', 'automation_executions', 'waiting_for',
  'executions name the action they wait for');
select throws_ok(
  $$ update app.automation_executions set waiting_for = '33f00000-0000-0000-0000-000000000001'
     where id = '33c00000-0000-0000-0000-000000000001' $$,
  '23514', null, 'only a waiting execution waits for an action');
select ok(not has_column_privilege('authenticated', 'app.automation_executions', 'waiting_for', 'select'),
  'a signed-in user does not read it directly');

-- The worker: its job's organization's executions only.
set local role htco_worker;
select set_config('htco.organization_id', '33a00000-0000-0000-0000-000000000000', true);
select lives_ok(
  $$ update app.automation_executions
     set state = 'waiting', waiting_for = '33f00000-0000-0000-0000-000000000001'
     where id = '33c00000-0000-0000-0000-000000000001' $$,
  'the worker records the wait');
update app.automation_executions
  set state = 'waiting', waiting_for = '33f00000-0000-0000-0000-000000000002'
  where id = '33c00000-0000-0000-0000-000000000002';
select lives_ok(
  $$ update app.automation_executions set state = 'retry_scheduled', waiting_for = null
     where id = '33c00000-0000-0000-0000-000000000001'
       and waiting_for is not distinct from '33f00000-0000-0000-0000-000000000001' $$,
  'leaving waiting clears it');
reset role;
select is((select waiting_for from app.automation_executions
           where id = '33c00000-0000-0000-0000-000000000001'),
  null, 'a resumed execution waits for nothing');
select is((select waiting_for from app.automation_executions
           where id = '33c00000-0000-0000-0000-000000000002'),
  null, 'the worker never wrote another organization''s run');

select * from finish();
rollback;
