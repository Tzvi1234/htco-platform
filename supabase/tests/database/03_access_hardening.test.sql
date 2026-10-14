-- Hardening after the review of slices 2.2 to 2.6: memberships are revoked, never deleted; shop
-- pages have a matching index; Owners and Admins can find an already registered user to add.
begin;
create extension if not exists pgtap with schema extensions;
select plan(9);

insert into auth.users (id, email) values
  ('31111111-0000-0000-0000-000000000001', 'owner-h@example.test'),
  ('31111111-0000-0000-0000-000000000002', 'support-h@example.test'),
  ('32222222-0000-0000-0000-000000000001', 'Elsewhere-H@Example.test');
insert into app.organizations (id, name) values
  ('3a000000-0000-0000-0000-000000000000', 'Organization H'),
  ('3b000000-0000-0000-0000-000000000000', 'Organization I');
insert into app.memberships (id, organization_id, user_id, role) values
  ('3e000000-0000-0000-0000-000000000001', '3a000000-0000-0000-0000-000000000000', '31111111-0000-0000-0000-000000000001', 'owner'),
  ('3e000000-0000-0000-0000-000000000002', '3a000000-0000-0000-0000-000000000000', '31111111-0000-0000-0000-000000000002', 'support'),
  ('3f000000-0000-0000-0000-000000000001', '3b000000-0000-0000-0000-000000000000', '32222222-0000-0000-0000-000000000001', 'owner');

select has_index('app', 'shops', 'shops_organization_id_id_idx', array['organization_id', 'id'],
  'shop pages filter by organization and follow the ID');

-- Owner H, signed in with MFA.
select set_config('request.jwt.claims', '{"sub":"31111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select throws_ok(
  $$ delete from app.memberships where id = '3e000000-0000-0000-0000-000000000002' $$,
  '42501', null, 'memberships cannot be deleted, only revoked (history stays)'
);
select is(
  app.registered_user_id('3a000000-0000-0000-0000-000000000000', 'elsewhere-h@example.test'),
  '32222222-0000-0000-0000-000000000001'::uuid,
  'an Owner with MFA finds a registered user by email, case-insensitively'
);
select is(
  app.registered_user_id('3a000000-0000-0000-0000-000000000000', 'nobody@example.test'), null,
  'an unknown email finds nobody'
);
select is(
  app.registered_user_id('3b000000-0000-0000-0000-000000000000', 'elsewhere-h@example.test'), null,
  'not for an organization the caller does not administer'
);
reset role;

select set_config('request.jwt.claims', '{"sub":"31111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is(
  app.registered_user_id('3a000000-0000-0000-0000-000000000000', 'elsewhere-h@example.test'), null,
  'not without MFA'
);
reset role;

select set_config('request.jwt.claims', '{"sub":"31111111-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is(
  app.registered_user_id('3a000000-0000-0000-0000-000000000000', 'elsewhere-h@example.test'), null,
  'not for a Support member'
);
reset role;

set local role anon;
select throws_ok(
  $$ select app.registered_user_id('3a000000-0000-0000-0000-000000000000', 'x@example.test') $$,
  '42501', null, 'anon cannot call it'
);
reset role;

select ok(
  (select prosecdef from pg_proc where oid = 'app.registered_user_id(uuid, text)'::regprocedure),
  'it is security definer, so callers never read auth.users themselves'
);

select * from finish();
rollback;
