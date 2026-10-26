-- A completed execution's handler output (#127): an object of at most 8 KiB, written by the
-- worker for its job's organization, and never readable by a signed-in user directly (the API
-- shows it to the Owner and Admins).
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(8);

insert into auth.users (id, email) values
  ('32e00000-0000-0000-0000-000000000001', 'owner-out@example.test');
insert into app.organizations (id, name) values
  ('32a00000-0000-0000-0000-000000000000', 'Organization Out'),
  ('32b00000-0000-0000-0000-000000000000', 'Organization Other');
insert into app.memberships (id, organization_id, user_id, role) values
  ('32d00000-0000-0000-0000-000000000001', '32a00000-0000-0000-0000-000000000000',
   '32e00000-0000-0000-0000-000000000001', 'owner');
insert into app.automation_definitions
  (id, version, name, business_owner, primary_trigger, callable_from, retry_max_attempts,
   retry_initial_backoff_seconds, retry_max_backoff_seconds, timeout_seconds, side_effect,
   approval_required, readiness, activation_phase, specification, document, specification_hash,
   registry_commit, synced_at)
values ('pgtap-output', 1, 'pgTAP output', 'owner', 'schedule', '{schedule}', 2, 1, 10, 30,
        'internal', false, 'draft', 5, '{}', '{}', repeat('a', 64), 'local', now());
insert into app.automation_executions
  (id, organization_id, automation_id, automation_version, idempotency_key, trigger, actor_kind,
   actor_id, state, correlation_id)
values
  ('32c00000-0000-0000-0000-000000000001', '32a00000-0000-0000-0000-000000000000',
   'pgtap-output', 1, 'out-1', 'schedule', 'system', 'scheduler', 'running', 'c-1'),
  ('32c00000-0000-0000-0000-000000000002', '32b00000-0000-0000-0000-000000000000',
   'pgtap-output', 1, 'out-2', 'schedule', 'system', 'scheduler', 'running', 'c-2');

select has_column('app', 'automation_executions', 'output', 'executions have an output column');
select throws_ok(
  $$ update app.automation_executions set output = '[1, 2]'
     where id = '32c00000-0000-0000-0000-000000000001' $$,
  '23514', null, 'an output is a JSON object');
select throws_ok(
  $$ update app.automation_executions
     set output = jsonb_build_object(
       'blob', (select string_agg(md5(i::text), '') from generate_series(1, 1000) as i))
     where id = '32c00000-0000-0000-0000-000000000001' $$,
  '23514', null, 'an output is at most 8 KiB');
select ok(not has_column_privilege('authenticated', 'app.automation_executions', 'output', 'select'),
  'a signed-in user never reads an output directly');
select ok(has_column_privilege('authenticated', 'app.automation_executions', 'idempotency_key', 'select'),
  'the other columns stay readable as before');

-- The worker: its job's organization's executions only.
set local role htco_worker;
select set_config('htco.organization_id', '32a00000-0000-0000-0000-000000000000', true);
select lives_ok(
  $$ update app.automation_executions set state = 'succeeded', output = '{"receipts": 2, "recorded": 1}'
     where id = '32c00000-0000-0000-0000-000000000001' $$,
  'the worker keeps a succeeded run''s output');
update app.automation_executions set output = '{"recorded": 9}'
  where id = '32c00000-0000-0000-0000-000000000002';
reset role;
select is((select output from app.automation_executions where id = '32c00000-0000-0000-0000-000000000001'),
  '{"receipts": 2, "recorded": 1}'::jsonb, 'the output is stored');
select is((select output from app.automation_executions where id = '32c00000-0000-0000-0000-000000000002'),
  null, 'nor for another organization''s run');

select * from finish();
rollback;
