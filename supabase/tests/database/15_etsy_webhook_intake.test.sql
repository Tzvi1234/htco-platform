-- The worker may create automation executions in its job's organization only (slice 5.3).
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(4);

insert into app.organizations (id, name) values
  ('5a000000-0000-0000-0000-000000000000', 'Organization W'),
  ('5b000000-0000-0000-0000-000000000000', 'Organization X');
insert into app.automation_definitions
  (id, version, name, business_owner, primary_trigger, callable_from, retry_max_attempts,
   retry_initial_backoff_seconds, retry_max_backoff_seconds, timeout_seconds, side_effect,
   approval_required, readiness, activation_phase, specification, document, specification_hash,
   registry_commit, synced_at)
values ('pgtap-intake', 1, 'pgTAP intake', 'owner', 'event', '{event}', 2, 1, 10, 30, 'internal',
        false, 'draft', 5, '{}', '{}', repeat('b', 64), 'local', now());

select ok(has_table_privilege('htco_worker', 'app.automation_executions', 'insert'),
  'the worker may insert executions');
select ok(not has_table_privilege('htco_worker', 'app.automation_executions', 'delete'),
  'the worker never deletes executions');

set local role htco_worker;
select set_config('htco.organization_id', '5a000000-0000-0000-0000-000000000000', true);

select lives_ok(
  $$insert into app.automation_executions
      (id, organization_id, automation_id, automation_version, idempotency_key, trigger,
       actor_kind, actor_id, state, attempts, correlation_id)
    values ('5c000000-0000-0000-0000-000000000001', '5a000000-0000-0000-0000-000000000000',
            'pgtap-intake', 1, 'etsy:shop:1:order.paid', 'event', 'system', 'etsy-webhook',
            'pending', 0, 'etsy-webhook:msg_1')$$,
  'an execution of the job''s organization is inserted'
);
select throws_ok(
  $$insert into app.automation_executions
      (id, organization_id, automation_id, automation_version, idempotency_key, trigger,
       actor_kind, actor_id, state, attempts, correlation_id)
    values ('5c000000-0000-0000-0000-000000000002', '5b000000-0000-0000-0000-000000000000',
            'pgtap-intake', 1, 'etsy:shop:2:order.paid', 'event', 'system', 'etsy-webhook',
            'pending', 0, 'etsy-webhook:msg_2')$$,
  '42501', null, 'an execution of another organization is refused'
);

reset role;
select * from finish();
rollback;
