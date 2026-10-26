-- Business tasks (slice 7.1): one row per idempotency key, the task machine checked again,
-- completed only with evidence, events append-only, members read their organization only, and
-- the worker reaches its job's organization only.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(16);

insert into auth.users (id, email) values
  ('35e00000-0000-0000-0000-000000000001', 'owner-t@example.test'),
  ('35e00000-0000-0000-0000-000000000002', 'owner-u@example.test');
insert into app.organizations (id, name) values
  ('35a00000-0000-0000-0000-000000000000', 'Organization T'),
  ('35b00000-0000-0000-0000-000000000000', 'Organization U');
insert into app.memberships (id, organization_id, user_id, role) values
  ('35d00000-0000-0000-0000-000000000001', '35a00000-0000-0000-0000-000000000000',
   '35e00000-0000-0000-0000-000000000001', 'owner'),
  ('35d00000-0000-0000-0000-000000000002', '35b00000-0000-0000-0000-000000000000',
   '35e00000-0000-0000-0000-000000000002', 'owner');

insert into app.tasks
  (id, organization_id, kind, title, assignee_kind, assignee_id, state, idempotency_key,
   correlation_id, created_at, updated_at)
values
  ('35c00000-0000-0000-0000-000000000001', '35a00000-0000-0000-0000-000000000000', 'human_channel',
   'Log in to Etsy again', 'user', '35e00000-0000-0000-0000-000000000001', 'running', 'hc:1', 'c-1',
   now(), now()),
  ('35c00000-0000-0000-0000-000000000002', '35b00000-0000-0000-0000-000000000000', 'general',
   'Weekly review', 'employee', 'maya', 'created', 'weekly:1', 'c-2', now(), now());
insert into app.task_events (id, organization_id, task_id, from_state, to_state, actor_kind, actor_id, occurred_at)
values ('35f00000-0000-0000-0000-000000000001', '35a00000-0000-0000-0000-000000000000',
        '35c00000-0000-0000-0000-000000000001', null, 'created', 'system', 'test', now());

select ok((select relrowsecurity from pg_class where oid = 'app.tasks'::regclass), 'tasks have RLS');
select ok((select relrowsecurity from pg_class where oid = 'app.task_events'::regclass), 'task events have RLS');
select throws_ok(
  $$ insert into app.tasks (id, organization_id, kind, title, assignee_kind, assignee_id, state,
       idempotency_key, correlation_id, created_at, updated_at)
     values ('35c00000-0000-0000-0000-000000000009', '35a00000-0000-0000-0000-000000000000', 'general',
       'Again', 'system', 's', 'created', 'hc:1', 'c', now(), now()) $$,
  '23505', null, 'one task per idempotency key in an organization (AC-7.1.2)');
select throws_ok(
  $$ insert into app.tasks (id, organization_id, kind, title, assignee_kind, assignee_id, state,
       idempotency_key, correlation_id, created_at, updated_at)
     values ('35c00000-0000-0000-0000-000000000008', '35a00000-0000-0000-0000-000000000000', 'chat',
       'New kind', 'system', 's', 'created', 'k:8', 'c', now(), now()) $$,
  '23514', null, 'a kind adds data, never a new shape: unknown kinds are refused');
select throws_ok(
  $$ update app.tasks set state = 'completed' where id = '35c00000-0000-0000-0000-000000000001' $$,
  '23514', null, 'completed only with its evidence (AC-7.1.3)');
select lives_ok(
  $$ update app.tasks set state = 'completed', evidence_kind = 'execution',
       evidence_reference = 'exec-1' where id = '35c00000-0000-0000-0000-000000000001' $$,
  'completed with its evidence');
select throws_ok(
  $$ update app.tasks set state = 'running' where id = '35c00000-0000-0000-0000-000000000001' $$,
  '23514', null, 'completed is final');
select throws_ok(
  $$ update app.tasks set state = 'running' where id = '35c00000-0000-0000-0000-000000000002' $$,
  '23514', null, 'created -> running skips the queue: refused');
select throws_ok(
  $$ insert into app.task_events (id, organization_id, task_id, from_state, to_state, actor_kind, actor_id, occurred_at)
     values ('35f00000-0000-0000-0000-000000000002', '35a00000-0000-0000-0000-000000000000',
       '35c00000-0000-0000-0000-000000000001', 'created', 'running', 'system', 't', now()) $$,
  '23514', null, 'an event follows the machine');
select throws_ok(
  $$ insert into app.task_events (id, organization_id, task_id, from_state, to_state, actor_kind, actor_id, occurred_at)
     values ('35f00000-0000-0000-0000-000000000003', '35b00000-0000-0000-0000-000000000000',
       '35c00000-0000-0000-0000-000000000001', null, 'created', 'system', 't', now()) $$,
  '23503', null, 'an event belongs to a task of its own organization');
select throws_ok(
  $$ update app.task_events set actor_id = 'x' where id = '35f00000-0000-0000-0000-000000000001' $$,
  '42501', null, 'task events are append-only');

-- A member reads their own organization's tasks only.
select set_config('request.jwt.claims', '{"sub":"35e00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::int from app.tasks), 1, 'a member sees their organization''s task only');
select is((select count(*)::int from app.task_events), 1, 'and its events');
select throws_ok(
  $$ update app.tasks set title = 'x' $$, '42501', null, 'a member never writes a task directly');
reset role;

-- The worker: its job's organization only.
set local role htco_worker;
select set_config('htco.organization_id', '35b00000-0000-0000-0000-000000000000', true);
select is((select count(*)::int from app.tasks), 1, 'the worker sees its job''s organization only');
select throws_ok(
  $$ update app.tasks set title = 'x' where id = '35c00000-0000-0000-0000-000000000002' $$,
  '42501', null, 'the worker moves a task, never retitles it');
reset role;

select * from finish();
rollback;
