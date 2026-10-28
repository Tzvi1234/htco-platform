-- Policy engine v1 (slice 7.2): versioned policies are never edited (AC-7.2.3), every decision
-- names the version it was made under, both tables are append-only, the Owner and Admins read
-- their organization's rows only, and the worker reads its job's policy and records its job's
-- decisions only.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(22);

insert into auth.users (id, email) values
  ('37e00000-0000-0000-0000-000000000001', 'owner-p@example.test'),
  ('37e00000-0000-0000-0000-000000000002', 'ops-p@example.test'),
  ('37e00000-0000-0000-0000-000000000003', 'owner-q@example.test');
insert into app.organizations (id, name) values
  ('37a00000-0000-0000-0000-000000000000', 'Organization P'),
  ('37b00000-0000-0000-0000-000000000000', 'Organization Q');
insert into app.memberships (id, organization_id, user_id, role) values
  ('37d00000-0000-0000-0000-000000000001', '37a00000-0000-0000-0000-000000000000',
   '37e00000-0000-0000-0000-000000000001', 'owner'),
  ('37d00000-0000-0000-0000-000000000002', '37a00000-0000-0000-0000-000000000000',
   '37e00000-0000-0000-0000-000000000002', 'operations'),
  ('37d00000-0000-0000-0000-000000000003', '37b00000-0000-0000-0000-000000000000',
   '37e00000-0000-0000-0000-000000000003', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('37f00000-0000-0000-0000-000000000001', '37a00000-0000-0000-0000-000000000000', 'Shop P', 'etsy', 'active');

insert into app.policies (id, organization_id, version, document, created_by_kind, created_by_id, created_at)
values
  ('37c00000-0000-0000-0000-000000000001', '37a00000-0000-0000-0000-000000000000', 1, '{"capabilities": {}}',
   'system', 'policy-baseline', now()),
  ('37c00000-0000-0000-0000-000000000002', '37a00000-0000-0000-0000-000000000000', 2, '{"capabilities": {}}',
   'user', '37e00000-0000-0000-0000-000000000001', now()),
  ('37c00000-0000-0000-0000-000000000003', '37b00000-0000-0000-0000-000000000000', 1, '{"capabilities": {}}',
   'system', 'policy-baseline', now());
insert into app.policy_decisions
  (id, organization_id, policy_version, shop_id, actor_kind, actor_id, capability, verdict, reason,
   correlation_id, decided_at)
values
  ('37900000-0000-0000-0000-000000000001', '37a00000-0000-0000-0000-000000000000', 1,
   '37f00000-0000-0000-0000-000000000001', 'automation', 'tracking-sync', 'orders.read', 'allow',
   'granted', 'c-1', now()),
  ('37900000-0000-0000-0000-000000000002', '37b00000-0000-0000-0000-000000000000', 1, null,
   'advisor', 'jev', 'orders.read', 'deny', 'advisor_has_no_capabilities', 'c-2', now());

select ok((select relrowsecurity from pg_class where oid = 'app.policies'::regclass), 'policies have RLS');
select ok((select relrowsecurity from pg_class where oid = 'app.policy_decisions'::regclass), 'decisions have RLS');

-- Versions (AC-7.2.3).
select throws_ok(
  $$ insert into app.policies (id, organization_id, version, document, created_by_kind, created_by_id, created_at)
     values ('37c00000-0000-0000-0000-000000000009', '37a00000-0000-0000-0000-000000000000', 2, '{}',
       'system', 's', now()) $$,
  '23505', null, 'one row per version in an organization');
select throws_ok(
  $$ insert into app.policies (id, organization_id, version, document, created_by_kind, created_by_id, created_at)
     values ('37c00000-0000-0000-0000-000000000008', '37a00000-0000-0000-0000-000000000000', 0, '{}',
       'system', 's', now()) $$,
  '23514', null, 'a version starts at 1');
select throws_ok(
  $$ insert into app.policies (id, organization_id, version, document, created_by_kind, created_by_id, created_at)
     values ('37c00000-0000-0000-0000-000000000007', '37a00000-0000-0000-0000-000000000000', 3, '[]',
       'system', 's', now()) $$,
  '23514', null, 'a document is a JSON object');
select throws_ok(
  $$ insert into app.policies (id, organization_id, version, document, created_by_kind, created_by_id, created_at)
     values ('37c00000-0000-0000-0000-000000000006', '37a00000-0000-0000-0000-000000000000', 3, '{}',
       'employee', 'maya', now()) $$,
  '23514', null, 'only a person or the platform publishes a policy');
select throws_ok(
  $$ update app.policies set document = '{"x": 1}' where id = '37c00000-0000-0000-0000-000000000001' $$,
  '42501', null, 'a published version never changes');
select throws_ok(
  $$ delete from app.policies where id = '37c00000-0000-0000-0000-000000000001' $$,
  '42501', null, 'a published version is never deleted');

-- Decisions name their version.
select throws_ok(
  $$ insert into app.policy_decisions (id, organization_id, policy_version, actor_kind, actor_id, capability,
       verdict, reason, correlation_id, decided_at)
     values ('37900000-0000-0000-0000-000000000009', '37a00000-0000-0000-0000-000000000000', 7,
       'system', 's', 'a.b', 'allow', 'granted', 'c', now()) $$,
  '23503', null, 'a decision names a version that exists in its organization');
select throws_ok(
  $$ insert into app.policy_decisions (id, organization_id, policy_version, actor_kind, actor_id, capability,
       verdict, reason, correlation_id, decided_at)
     values ('37900000-0000-0000-0000-000000000008', '37a00000-0000-0000-0000-000000000000', 1,
       'advisor', 'jev', 'a.b', 'allow', 'advisor_has_no_capabilities', 'c', now()) $$,
  '23514', null, 'the reason fixes the verdict');
select throws_ok(
  $$ insert into app.policy_decisions (id, organization_id, policy_version, actor_kind, actor_id, capability,
       verdict, reason, correlation_id, decided_at)
     values ('37900000-0000-0000-0000-000000000007', '37a00000-0000-0000-0000-000000000000', null,
       'system', 's', 'a.b', 'allow', 'granted', 'c', now()) $$,
  '23514', null, 'only a missing policy leaves the version empty');
select lives_ok(
  $$ insert into app.policy_decisions (id, organization_id, policy_version, actor_kind, actor_id, capability,
       verdict, reason, correlation_id, decided_at)
     values ('37900000-0000-0000-0000-000000000006', '37a00000-0000-0000-0000-000000000000', null,
       'system', 's', '', 'deny', 'no_policy', 'c', now()) $$,
  'a denial without a policy, even of an empty capability name, is recorded');
select throws_ok(
  $$ update app.policy_decisions set reason = 'granted' where id = '37900000-0000-0000-0000-000000000001' $$,
  '42501', null, 'decisions are append-only');
select throws_ok(
  $$ delete from app.policy_decisions where id = '37900000-0000-0000-0000-000000000001' $$,
  '42501', null, 'decisions are never deleted');

-- Members: the Owner and Admins read their own organization; no member writes.
select set_config('request.jwt.claims', '{"sub":"37e00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*)::int from app.policies), 2, 'the Owner reads their organization''s versions only');
select is((select count(*)::int from app.policy_decisions), 2, 'and its decisions only');
select throws_ok(
  $$ insert into app.policies (id, organization_id, version, document, created_by_kind, created_by_id, created_at)
     values ('37c00000-0000-0000-0000-000000000005', '37a00000-0000-0000-0000-000000000000', 3, '{}',
       'user', '37e00000-0000-0000-0000-000000000001', now()) $$,
  '42501', null, 'a policy is published only through the API''s checked use case');
reset role;
select set_config('request.jwt.claims', '{"sub":"37e00000-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*)::int from app.policies), 0, 'an Operations member reads no policy');
reset role;

-- The worker: its job's organization only, reading versions and recording decisions.
set local role htco_worker;
select set_config('htco.organization_id', '37b00000-0000-0000-0000-000000000000', true);
select is((select count(*)::int from app.policies), 1, 'the worker reads its job''s policy only');
select lives_ok(
  $$ insert into app.policy_decisions (id, organization_id, policy_version, actor_kind, actor_id, capability,
       verdict, reason, correlation_id, decided_at)
     values ('37900000-0000-0000-0000-000000000005', '37b00000-0000-0000-0000-000000000000', 1,
       'automation', 'tracking-sync', 'orders.read', 'deny', 'not_granted', 'c', now()) $$,
  'the worker records its job''s decision');
select throws_ok(
  $$ insert into app.policy_decisions (id, organization_id, policy_version, actor_kind, actor_id, capability,
       verdict, reason, correlation_id, decided_at)
     values ('37900000-0000-0000-0000-000000000004', '37a00000-0000-0000-0000-000000000000', 1,
       'automation', 'tracking-sync', 'orders.read', 'allow', 'granted', 'c', now()) $$,
  '42501', null, 'never another organization''s');
select throws_ok(
  $$ insert into app.policies (id, organization_id, version, document, created_by_kind, created_by_id, created_at)
     values ('37c00000-0000-0000-0000-000000000004', '37b00000-0000-0000-0000-000000000000', 2, '{}',
       'system', 'worker', now()) $$,
  '42501', null, 'the worker never publishes a policy');
reset role;

select * from finish();
rollback;
