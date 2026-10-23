-- Notifications and email (slice 6.4): one notification per recipient and key, members read and
-- mark only their own, owners and admins see email deliveries, and the worker reaches members
-- only through its two functions, inside its job's organization.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(13);

insert into auth.users (id, email) values
  ('a1111111-0000-0000-0000-000000000001', 'owner-n@example.test'),
  ('a1111111-0000-0000-0000-000000000002', 'ops-n@example.test'),
  ('a2222222-0000-0000-0000-000000000001', 'owner-o@example.test');
insert into app.organizations (id, name) values
  ('aa000000-0000-0000-0000-000000000000', 'Organization N'),
  ('ab000000-0000-0000-0000-000000000000', 'Organization O');
insert into app.memberships (id, organization_id, user_id, role) values
  ('ae000000-0000-0000-0000-000000000001', 'aa000000-0000-0000-0000-000000000000', 'a1111111-0000-0000-0000-000000000001', 'owner'),
  ('ae000000-0000-0000-0000-000000000002', 'aa000000-0000-0000-0000-000000000000', 'a1111111-0000-0000-0000-000000000002', 'operations'),
  ('af000000-0000-0000-0000-000000000001', 'ab000000-0000-0000-0000-000000000000', 'a2222222-0000-0000-0000-000000000001', 'owner');
insert into app.notifications (id, organization_id, user_id, kind, params, dedup_key, created_at) values
  (gen_random_uuid(), 'aa000000-0000-0000-0000-000000000000', 'a1111111-0000-0000-0000-000000000001', 'incident_opened', '{"receipt_id":"7"}', 'incident_opened:1', now()),
  (gen_random_uuid(), 'aa000000-0000-0000-0000-000000000000', 'a1111111-0000-0000-0000-000000000002', 'incident_opened', '{"receipt_id":"7"}', 'incident_opened:1', now());
insert into app.email_deliveries (id, outbox_id, organization_id, user_id, kind, status, error, created_at) values
  (gen_random_uuid(), gen_random_uuid(), 'aa000000-0000-0000-0000-000000000000', 'a1111111-0000-0000-0000-000000000001', 'incident_opened', 'failed', 'resend 422', now());

select throws_ok(
  $$insert into app.notifications (id, organization_id, user_id, kind, params, dedup_key, created_at)
    values (gen_random_uuid(), 'aa000000-0000-0000-0000-000000000000',
            'a1111111-0000-0000-0000-000000000001', 'incident_opened', '{}', 'incident_opened:1', now())$$,
  '23505', null, 'one notification per recipient and key'
);
select throws_ok(
  $$insert into app.email_deliveries (id, outbox_id, organization_id, user_id, kind, status, error, created_at)
    values (gen_random_uuid(), gen_random_uuid(), 'aa000000-0000-0000-0000-000000000000',
            'a1111111-0000-0000-0000-000000000001', 'incident_opened', 'failed', 'bad owner-n@example.test', now())$$,
  '23514', null, 'a delivery error never holds an address'
);

-- Members --------------------------------------------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"a1111111-0000-0000-0000-000000000002","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::integer from app.notifications), 1, 'a member sees only their own notifications');
select is((select count(*)::integer from app.email_deliveries), 0, 'operations do not see email deliveries');
update app.notifications set read_at = now();
select is((select count(*)::integer from app.notifications where read_at is not null), 1, 'a member marks their own read');
select throws_ok(
  $$update app.notifications set params = '{}'$$,
  '42501', null, 'a member changes nothing else'
);
reset role;
select is(
  (select count(*)::integer from app.notifications where read_at is not null), 1,
  'the other member''s notification stayed unread'
);
select set_config('request.jwt.claims', '{"sub":"a1111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::integer from app.email_deliveries), 1, 'the owner sees the failed delivery');
reset role;

select ok(not has_function_privilege('authenticated', 'app.worker_member_email(uuid)', 'execute'),
  'members cannot look up addresses');

-- The worker -----------------------------------------------------------------------------------
set local role htco_worker;
select set_config('htco.organization_id', 'aa000000-0000-0000-0000-000000000000', true);
select is(
  (select count(*)::integer from app.worker_notification_recipients(array['owner'], null, null)), 1,
  'the worker finds the owners of its organization'
);
select is(
  app.worker_member_email('a1111111-0000-0000-0000-000000000001'), 'owner-n@example.test',
  'and a member''s address at send time'
);
select is(
  app.worker_member_email('a2222222-0000-0000-0000-000000000001'), null,
  'never the address of another organization''s member'
);
select throws_ok(
  $$select email from auth.users$$, '42501', null, 'the worker reads no identity table directly'
);

reset role;
select * from finish();
rollback;
