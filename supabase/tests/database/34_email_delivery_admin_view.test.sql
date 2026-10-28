-- The failed-email admin view (AC-6.4.3): a delivery names its shop, incident and order, the
-- worker writes them, and the recipient's address reaches an Owner or Admin only masked, through
-- `app.admin_member_email_masked`; nobody else gets even that.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(10);

insert into auth.users (id, email) values
  ('a5555555-0000-0000-0000-000000000001', 'owner.x@example.test'),
  ('a5555555-0000-0000-0000-000000000002', 'admin-x@mail.example.test'),
  ('a5555555-0000-0000-0000-000000000003', 'ops-x@example.test'),
  ('a6666666-0000-0000-0000-000000000001', 'owner-y@example.test');
insert into app.organizations (id, name) values
  ('ca000000-0000-0000-0000-000000000000', 'Organization X'),
  ('cb000000-0000-0000-0000-000000000000', 'Organization Y');
insert into app.memberships (id, organization_id, user_id, role) values
  ('ce000000-0000-0000-0000-000000000001', 'ca000000-0000-0000-0000-000000000000', 'a5555555-0000-0000-0000-000000000001', 'owner'),
  ('ce000000-0000-0000-0000-000000000002', 'ca000000-0000-0000-0000-000000000000', 'a5555555-0000-0000-0000-000000000002', 'admin'),
  ('ce000000-0000-0000-0000-000000000003', 'ca000000-0000-0000-0000-000000000000', 'a5555555-0000-0000-0000-000000000003', 'operations'),
  ('cf000000-0000-0000-0000-000000000001', 'cb000000-0000-0000-0000-000000000000', 'a6666666-0000-0000-0000-000000000001', 'owner');

-- The worker records a failure with what it was about ---------------------------------------------
set local role htco_worker;
select set_config('htco.organization_id', 'ca000000-0000-0000-0000-000000000000', true);
select lives_ok(
  $$insert into app.email_deliveries
      (id, outbox_id, organization_id, user_id, kind, status, error, created_at, shop_id, incident_id, receipt_id)
    values ('cd000000-0000-7000-8000-000000000001', gen_random_uuid(), 'ca000000-0000-0000-0000-000000000000',
            'a5555555-0000-0000-0000-000000000002', 'incident_opened', 'failed', 'resend 422', now(),
            'cc000000-0000-0000-0000-000000000001', 'c9000000-0000-0000-0000-000000000001', 51)$$,
  'the worker records the shop, incident and order of a failed email'
);
reset role;
select throws_ok(
  $$insert into app.email_deliveries (id, outbox_id, organization_id, user_id, kind, status, created_at, receipt_id)
    values (gen_random_uuid(), gen_random_uuid(), 'ca000000-0000-0000-0000-000000000000',
            'a5555555-0000-0000-0000-000000000001', 'incident_opened', 'failed', now(), 0)$$,
  '23514', null, 'an order ID is positive'
);

-- An Owner and an Admin --------------------------------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"a5555555-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select results_eq(
  $$ select shop_id, incident_id, receipt_id from app.email_deliveries $$,
  $$ values ('cc000000-0000-0000-0000-000000000001'::uuid, 'c9000000-0000-0000-0000-000000000001'::uuid, 51::bigint) $$,
  'the owner sees what the failed email was about'
);
select is(
  app.admin_member_email_masked('ca000000-0000-0000-0000-000000000000', 'a5555555-0000-0000-0000-000000000002'),
  'a***@m***.test', 'the owner sees the recipient''s address masked, never whole'
);
select is(
  app.admin_member_email_masked('ca000000-0000-0000-0000-000000000000', 'a6666666-0000-0000-0000-000000000001'),
  null, 'never the address of another organization''s member'
);
select is(
  app.admin_member_email_masked('cb000000-0000-0000-0000-000000000000', 'a6666666-0000-0000-0000-000000000001'),
  null, 'nor of an organization the caller does not administer'
);
reset role;
select set_config('request.jwt.claims', '{"sub":"a5555555-0000-0000-0000-000000000002","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is(
  app.admin_member_email_masked('ca000000-0000-0000-0000-000000000000', 'a5555555-0000-0000-0000-000000000001'),
  'o***@e***.test', 'an admin too'
);
reset role;

-- Every other role -------------------------------------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"a5555555-0000-0000-0000-000000000003","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::integer from app.email_deliveries), 0, 'operations see no delivery');
select is(
  app.admin_member_email_masked('ca000000-0000-0000-0000-000000000000', 'a5555555-0000-0000-0000-000000000001'),
  null, 'and no address, not even masked'
);
reset role;
select ok(
  not has_function_privilege('anon', 'app.admin_member_email_masked(uuid, uuid)', 'execute'),
  'anonymous callers cannot call it'
);

select * from finish();
rollback;
