-- Activity records (slice 7.7): a monthly-partitioned, append-only store (AC-7.7.2); members see
-- their organization's records, a shop's record only with that shop, and an organization-wide
-- record only as an Owner or Admin (AC-7.7.3); identifier columns hold no free text (AC-7.7.4);
-- the worker appends its job's organization's records only; partitions are never reached
-- directly.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(32);

insert into auth.users (id, email) values
  ('41e00000-0000-0000-0000-000000000001', 'owner-a@example.test'),
  ('41e00000-0000-0000-0000-000000000002', 'support-a@example.test'),
  ('41e00000-0000-0000-0000-000000000003', 'owner-b@example.test'),
  ('41e00000-0000-0000-0000-000000000004', 'admin-a@example.test');
insert into app.organizations (id, name) values
  ('41a00000-0000-0000-0000-000000000000', 'Organization A'),
  ('41b00000-0000-0000-0000-000000000000', 'Organization B');
insert into app.memberships (id, organization_id, user_id, role) values
  ('41d00000-0000-0000-0000-000000000001', '41a00000-0000-0000-0000-000000000000',
   '41e00000-0000-0000-0000-000000000001', 'owner'),
  ('41d00000-0000-0000-0000-000000000002', '41a00000-0000-0000-0000-000000000000',
   '41e00000-0000-0000-0000-000000000002', 'support'),
  ('41d00000-0000-0000-0000-000000000003', '41b00000-0000-0000-0000-000000000000',
   '41e00000-0000-0000-0000-000000000003', 'owner'),
  ('41d00000-0000-0000-0000-000000000004', '41a00000-0000-0000-0000-000000000000',
   '41e00000-0000-0000-0000-000000000004', 'admin');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('41f00000-0000-0000-0000-000000000001', '41a00000-0000-0000-0000-000000000000', 'Shop A1', 'etsy', 'active'),
  ('41f00000-0000-0000-0000-000000000002', '41a00000-0000-0000-0000-000000000000', 'Shop A2', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('41900000-0000-0000-0000-000000000001', '41a00000-0000-0000-0000-000000000000',
   '41d00000-0000-0000-0000-000000000002', '41f00000-0000-0000-0000-000000000001');

insert into app.activity_records
  (id, organization_id, shop_id, kind, actor_kind, actor_id, action, outcome, summary_template,
   summary, occurred_at, correlation_id)
values
  ('41c00000-0000-0000-0000-000000000001', '41a00000-0000-0000-0000-000000000000', null,
   'business_event', 'system', 'scheduler', 'reports.sent', 'succeeded', 'notification.sent',
   'Sent notification n-1 of kind daily', '2026-10-27 09:00+00', 'c-1'),
  ('41c00000-0000-0000-0000-000000000002', '41a00000-0000-0000-0000-000000000000',
   '41f00000-0000-0000-0000-000000000001', 'incident', 'automation', 'tracking-sync',
   'incidents.opened', 'succeeded', 'incident.opened', 'Opened incident i-1 for receipt 1',
   '2026-10-27 09:00+00', 'c-2'),
  ('41c00000-0000-0000-0000-000000000003', '41a00000-0000-0000-0000-000000000000',
   '41f00000-0000-0000-0000-000000000002', 'incident', 'automation', 'tracking-sync',
   'incidents.opened', 'succeeded', 'incident.opened', 'Opened incident i-2 for receipt 2',
   '2026-11-02 09:00+00', 'c-3'),
  ('41c00000-0000-0000-0000-000000000004', '41b00000-0000-0000-0000-000000000000', null,
   'task_execution', 'employee', 'maya', 'tasks.moved', 'failed', 'task.moved',
   'Task t-1 moved from running to failed', '2026-10-27 09:00+00', 'c-4');

select is((select relkind::text from pg_class where oid = 'app.activity_records'::regclass), 'p',
  'activity records are partitioned');
select ok((select relrowsecurity from pg_class where oid = 'app.activity_records'::regclass),
  'the parent has RLS');
select ok((select bool_and(c.relrowsecurity) from pg_inherits i join pg_class c on c.oid = i.inhrelid
           where i.inhparent = 'app.activity_records'::regclass),
  'every partition has RLS');
select is((select tableoid::regclass::text from app.activity_records where id = '41c00000-0000-0000-0000-000000000003'),
  'app.activity_records_2026_11', 'a record lands in its month''s partition');
select lives_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000009', '41a00000-0000-0000-0000-000000000000', 'decision',
       'system', 's', 'policy.decided', 'denied', 'policy.decided', 'Policy 1 answered deny for x',
       '2031-01-01 00:00+00', 'c') $$,
  'a month without its partition still lands (the default partition)');

-- Domain rules again in the database (AC-7.7.2).
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000010', '41a00000-0000-0000-0000-000000000000', 'decision',
       'system', 's', 'policy.decided', 'denied', 'policy.decided', repeat('x', 501), now(), 'c') $$,
  '23514', null, 'a summary is at most 500 characters');
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000011', '41a00000-0000-0000-0000-000000000000', 'decision',
       'system', 's', 'policy.decided', 'denied', 'policy.decided', 'ok', now(), ' ') $$,
  '23514', null, 'a record needs a correlation id');
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000012', '41a00000-0000-0000-0000-000000000000', 'chat',
       'system', 's', 'policy.decided', 'denied', 'policy.decided', 'ok', now(), 'c') $$,
  '23514', null, 'the kind is one of ActivityKind');
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000013', '41a00000-0000-0000-0000-000000000000', 'decision',
       'system', 's', 'policy.decided', 'done', 'policy.decided', 'ok', now(), 'c') $$,
  '23514', null, 'the outcome is one of ActivityOutcome');
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000014', '41a00000-0000-0000-0000-000000000000', 'decision',
       'system', 's', 'policy.decided', 'denied', 'Free text', 'ok', now(), 'c') $$,
  '23514', null, 'a summary names the code template that produced it');
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, shop_id, kind, actor_kind, actor_id, action,
       outcome, summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000015', '41b00000-0000-0000-0000-000000000000',
       '41f00000-0000-0000-0000-000000000001', 'decision', 'system', 's', 'policy.decided', 'denied',
       'policy.decided', 'ok', now(), 'c') $$,
  '23503', null, 'a shop of another organization is refused');
-- No free text in an identifier column (AC-7.7.4, sage2 on #191): a space, an address, a newline.
select throws_ok(
  format(
    $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action,
         outcome, summary_template, summary, occurred_at, correlation_id, causation_id)
       values (gen_random_uuid(), '41a00000-0000-0000-0000-000000000000', 'decision', 'system',
         %L, 'policy.decided', 'denied', 'policy.decided', 'ok', now(), %L, %L) $$,
    v.actor_id, v.correlation_id, v.causation_id
  ),
  '23514', null, format('the %s is an identifier, never %s', v.column_name, v.label)
)
from (values
  ('actor id', 'a space', 'a b', 'c', null),
  ('actor id', 'an address', 'dana@example.test', 'c', null),
  ('actor id', 'a newline', E'a\n', 'c', null),
  ('correlation id', 'a space', 's', 'c d', null),
  ('correlation id', 'an address', 's', 'dana@example.test', null),
  ('correlation id', 'a newline', 's', E'c\n', null),
  ('causation id', 'a space', 's', 'c', 'e f'),
  ('causation id', 'an address', 's', 'c', 'dana@example.test'),
  ('causation id', 'a newline', 's', 'c', E'e\n')
) as v (column_name, label, actor_id, correlation_id, causation_id);
select throws_ok(
  $$ update app.activity_records set summary = 'x' where id = '41c00000-0000-0000-0000-000000000001' $$,
  '42501', null, 'records are append-only, even for the owner of the table');
select throws_ok(
  $$ delete from app.activity_records where id = '41c00000-0000-0000-0000-000000000001' $$,
  '42501', null, 'records are never deleted one by one');

-- Members (AC-7.7.3).
select set_config('request.jwt.claims', '{"sub":"41e00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*)::int from app.activity_records), 4,
  'the Owner sees every record of their organization and none of another''s');
reset role;
select set_config('request.jwt.claims', '{"sub":"41e00000-0000-0000-0000-000000000002","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select array_agg(correlation_id order by correlation_id) from app.activity_records),
  array['c-2'], 'a shop role sees its granted shop''s records only, never organization-wide ones');
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000016', '41a00000-0000-0000-0000-000000000000', 'decision',
       'user', 'u', 'policy.decided', 'denied', 'policy.decided', 'ok', now(), 'c') $$,
  '42501', null, 'a member never writes a record directly');
select throws_ok(
  $$ select count(*) from app.activity_records_2026_10 $$,
  '42501', null, 'a partition is never read directly');
reset role;
select set_config('request.jwt.claims', '{"sub":"41e00000-0000-0000-0000-000000000004","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select array_agg(correlation_id order by correlation_id) from app.activity_records
           where shop_id is null),
  array['c', 'c-1'], 'an Admin sees the organization-wide records');
reset role;
select set_config('request.jwt.claims', '{"sub":"41e00000-0000-0000-0000-000000000003","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*)::int from app.activity_records), 1, 'organization B sees only its own record');
reset role;

-- The worker: appends its job's organization's records only.
set local role htco_worker;
select set_config('htco.organization_id', '41b00000-0000-0000-0000-000000000000', true);
select lives_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000017', '41b00000-0000-0000-0000-000000000000',
       'automation_execution', 'automation', 'etsy-order-intake', 'automations.finished', 'succeeded',
       'automation.finished', 'Automation etsy-order-intake run r-1 ended succeeded', now(), 'w-1') $$,
  'the worker appends its job''s record');
select throws_ok(
  $$ insert into app.activity_records (id, organization_id, kind, actor_kind, actor_id, action, outcome,
       summary_template, summary, occurred_at, correlation_id)
     values ('41c00000-0000-0000-0000-000000000018', '41a00000-0000-0000-0000-000000000000',
       'automation_execution', 'automation', 'etsy-order-intake', 'automations.finished', 'succeeded',
       'automation.finished', 'x', now(), 'w-2') $$,
  '42501', null, 'never another organization''s');
select is((select count(*)::int from app.activity_records), 2, 'the worker reads its job''s records only');
select throws_ok(
  $$ update app.activity_records set outcome = 'failed' where correlation_id = 'w-1' $$,
  '42501', null, 'the worker never changes a record');
reset role;

select * from finish();
rollback;
