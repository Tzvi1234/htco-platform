-- Identity and access tables with row-level security (slice 2.2, AC-2.2.1 to AC-2.2.5).
--
-- Two organizations: A (owner, and a Support user granted shop A1 only) and B (owner).
-- Each block signs in by setting the JWT claims Supabase's `auth.uid()` and `auth.jwt()` read,
-- then switches to the `authenticated` role, exactly as the API's user-scoped transactions do.
begin;
create extension if not exists pgtap with schema extensions;
select plan(49);

-- Fixtures (as the migration owner, which RLS does not restrict) -------------------------------

insert into auth.users (id, email) values
  ('11111111-0000-0000-0000-000000000001', 'owner-a@example.test'),
  ('11111111-0000-0000-0000-000000000002', 'support-a@example.test'),
  ('22222222-0000-0000-0000-000000000001', 'owner-b@example.test');

insert into app.organizations (id, name) values
  ('a0000000-0000-0000-0000-000000000000', 'Organization A'),
  ('b0000000-0000-0000-0000-000000000000', 'Organization B');

insert into app.user_profiles (user_id, display_name) values
  ('11111111-0000-0000-0000-000000000001', 'Owner A'),
  ('11111111-0000-0000-0000-000000000002', 'Support A'),
  ('22222222-0000-0000-0000-000000000001', 'Owner B');

insert into app.memberships (id, organization_id, user_id, role) values
  ('ae000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000000', '11111111-0000-0000-0000-000000000001', 'owner'),
  ('ae000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000000', '11111111-0000-0000-0000-000000000002', 'support'),
  ('be000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000000', '22222222-0000-0000-0000-000000000001', 'owner');

insert into app.shops (id, organization_id, name, marketplace, status) values
  ('a1000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000000', 'Shop A1', 'etsy', 'pending'),
  ('a2000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000000', 'Shop A2', 'etsy', 'pending'),
  ('b1000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000000', 'Shop B1', 'etsy', 'pending');

insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('ac000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000000',
   'ae000000-0000-0000-0000-000000000002', 'a1000000-0000-0000-0000-000000000001');

insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id) values
  ('ad000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000000', 'system', 'test', 'membership.created', 'membership', 'ae000000-0000-0000-0000-000000000001'),
  ('bd000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000000', 'system', 'test', 'membership.created', 'membership', 'be000000-0000-0000-0000-000000000001');

-- Tables exist; AC-2.2.1: every table in `app` and `audit` has RLS -------------------------------

select has_table('app', 'organizations', 'app.organizations exists');
select has_table('app', 'user_profiles', 'app.user_profiles exists');
select has_table('app', 'memberships', 'app.memberships exists');
select has_table('app', 'shops', 'app.shops exists');
select has_table('app', 'shop_access', 'app.shop_access exists');
select has_table('audit', 'audit_log', 'audit.audit_log exists');

select is_empty(
  $$ select n.nspname || '.' || c.relname
       from pg_catalog.pg_class c
       join pg_catalog.pg_namespace n on n.oid = c.relnamespace
      where n.nspname in ('app', 'audit')
        and c.relkind in ('r', 'p')
        and not c.relrowsecurity $$,
  'AC-2.2.1: every table in app and audit has row-level security enabled'
);

-- Roles and actor kinds are closed lists ---------------------------------------------------------

select throws_ok(
  $$ insert into app.memberships (id, organization_id, user_id, role)
     values ('ae000000-0000-0000-0000-0000000000ff', 'b0000000-0000-0000-0000-000000000000', '11111111-0000-0000-0000-000000000002', 'superuser') $$,
  '23514', null, 'a membership role outside the seven roles is rejected'
);
select throws_ok(
  $$ insert into app.shop_access (id, organization_id, membership_id, shop_id)
     values ('ac000000-0000-0000-0000-0000000000ff', 'a0000000-0000-0000-0000-000000000000',
             'ae000000-0000-0000-0000-000000000002', 'b1000000-0000-0000-0000-000000000001') $$,
  '23503', null, 'a shop grant cannot point at another organization''s shop'
);
select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values ('ad000000-0000-0000-0000-0000000000ff', 'a0000000-0000-0000-0000-000000000000', 'robot', 'x', 'a', 'b', 'c') $$,
  '23514', null, 'an audit record with an unknown actor kind is rejected'
);
select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values ('ad000000-0000-0000-0000-0000000000fe', 'a0000000-0000-0000-0000-000000000000', 'user', '  ', 'a', 'b', 'c') $$,
  '23514', null, 'an audit record with a blank actor id is rejected'
);

-- AC-2.2.2: a user of organization A sees nothing of organization B ------------------------------

select set_config('request.jwt.claims', '{"sub":"11111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;

select results_eq($$ select id from app.organizations $$, $$ values ('a0000000-0000-0000-0000-000000000000'::uuid) $$,
  'owner A sees only organization A');
select is((select count(*)::int from app.memberships where organization_id = 'b0000000-0000-0000-0000-000000000000'), 0,
  'owner A sees no membership of B');
select is((select count(*)::int from app.memberships), 2, 'owner A sees both memberships of A');
select is((select count(*)::int from app.shops where organization_id = 'b0000000-0000-0000-0000-000000000000'), 0,
  'owner A sees no shop of B');
select is((select count(*)::int from app.shops), 2, 'owner A sees every shop of A');
select is((select count(*)::int from app.shop_access where organization_id = 'b0000000-0000-0000-0000-000000000000'), 0,
  'owner A sees no shop grant of B');
select is((select count(*)::int from app.shop_access), 1, 'owner A sees the shop grants of A');
select is((select count(*)::int from app.user_profiles where user_id = '22222222-0000-0000-0000-000000000001'), 0,
  'owner A sees no profile of a user of B');
select is((select count(*)::int from app.user_profiles), 2, 'owner A sees the profiles of A''s members');
select results_eq($$ select id from audit.audit_log $$, $$ values ('ad000000-0000-0000-0000-000000000001'::uuid) $$,
  'owner A sees only A''s audit records');

-- Writes into another organization are refused, not silently applied.
select throws_ok(
  $$ insert into app.shops (id, organization_id, name, marketplace, status)
     values ('b9000000-0000-0000-0000-000000000009', 'b0000000-0000-0000-0000-000000000000', 'Intruder', 'etsy', 'pending') $$,
  '42501', null, 'owner A cannot create a shop in organization B'
);
select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values ('bd000000-0000-0000-0000-0000000000ff', 'b0000000-0000-0000-0000-000000000000', 'user', '11111111-0000-0000-0000-000000000001', 'a', 'b', 'c') $$,
  '42501', null, 'owner A cannot write audit records for organization B'
);
update app.memberships set role = 'viewer' where id = 'be000000-0000-0000-0000-000000000001';
reset role;
select is((select role from app.memberships where id = 'be000000-0000-0000-0000-000000000001'), 'owner',
  'owner A''s update of a membership of B changed nothing');

-- The same from the other side.
select set_config('request.jwt.claims', '{"sub":"22222222-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select id from app.organizations $$, $$ values ('b0000000-0000-0000-0000-000000000000'::uuid) $$,
  'owner B sees only organization B');
select results_eq($$ select id from app.shops $$, $$ values ('b1000000-0000-0000-0000-000000000001'::uuid) $$,
  'owner B sees only the shop of B');
select is((select count(*)::int from app.shop_access), 0, 'owner B sees no shop grant of A');
select results_eq($$ select id from audit.audit_log $$, $$ values ('bd000000-0000-0000-0000-000000000001'::uuid) $$,
  'owner B sees only B''s audit records');
reset role;

-- AC-2.2.3: a Support user granted shop A1 only cannot read shop A2 ----------------------------

select set_config('request.jwt.claims', '{"sub":"11111111-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select id from app.shops $$, $$ values ('a1000000-0000-0000-0000-000000000001'::uuid) $$,
  'AC-2.2.3: Support A sees shop A1 and not shop A2');
select results_eq($$ select id from app.shop_access $$, $$ values ('ac000000-0000-0000-0000-000000000001'::uuid) $$,
  'Support A sees only its own shop grant');
select is((select count(*)::int from audit.audit_log), 0, 'Support A cannot read the audit log');
select throws_ok(
  $$ insert into app.shops (id, organization_id, name, marketplace, status)
     values ('a9000000-0000-0000-0000-000000000009', 'a0000000-0000-0000-0000-000000000000', 'New', 'etsy', 'pending') $$,
  '42501', null, 'Support A cannot create shops, even with MFA'
);
select throws_ok(
  $$ insert into app.shop_access (id, organization_id, membership_id, shop_id)
     values ('ac000000-0000-0000-0000-000000000009', 'a0000000-0000-0000-0000-000000000000',
             'ae000000-0000-0000-0000-000000000002', 'a2000000-0000-0000-0000-000000000002') $$,
  '42501', null, 'Support A cannot grant itself another shop'
);
reset role;

-- AC-2.2.4: nobody can change or remove audit records ------------------------------------------

select set_config('request.jwt.claims', '{"sub":"11111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select lives_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values ('ad000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000000', 'user', '11111111-0000-0000-0000-000000000001', 'shop.viewed', 'shop', 'a1') $$,
  'owner A can append an audit record for organization A'
);
select throws_ok($$ update audit.audit_log set action = 'x' $$, '42501', null,
  'AC-2.2.4: authenticated cannot update audit records');
select throws_ok($$ delete from audit.audit_log $$, '42501', null,
  'AC-2.2.4: authenticated cannot delete audit records');
reset role;

set local role service_role;
select throws_ok($$ update audit.audit_log set action = 'x' $$, '42501', null,
  'AC-2.2.4: service_role cannot update audit records');
select throws_ok($$ delete from audit.audit_log $$, '42501', null,
  'AC-2.2.4: service_role cannot delete audit records');
select throws_ok($$ truncate audit.audit_log $$, '42501', null,
  'AC-2.2.4: service_role cannot truncate the audit log');
reset role;

select throws_ok($$ update audit.audit_log set action = 'x' $$, '42501', null,
  'even the table owner cannot update audit records (trigger)');
select throws_ok($$ delete from audit.audit_log $$, '42501', null,
  'even the table owner cannot delete audit records (trigger)');

-- AC-2.2.5: owner-administered tables reject writes from an aal1 session -----------------------

select set_config('request.jwt.claims', '{"sub":"11111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::int from app.shops), 2, 'owner A can still read with an aal1 session');
select throws_ok(
  $$ insert into app.shops (id, organization_id, name, marketplace, status)
     values ('a3000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000000', 'Shop A3', 'etsy', 'pending') $$,
  '42501', null, 'AC-2.2.5: owner A cannot create a shop with an aal1 session'
);
select throws_ok(
  $$ insert into app.shop_access (id, organization_id, membership_id, shop_id)
     values ('ac000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000000',
             'ae000000-0000-0000-0000-000000000002', 'a2000000-0000-0000-0000-000000000002') $$,
  '42501', null, 'AC-2.2.5: owner A cannot grant shop access with an aal1 session'
);
update app.memberships set role = 'viewer' where id = 'ae000000-0000-0000-0000-000000000002';
reset role;
select is((select role from app.memberships where id = 'ae000000-0000-0000-0000-000000000002'), 'support',
  'AC-2.2.5: owner A''s role change with an aal1 session changed nothing');

select set_config('request.jwt.claims', '{"sub":"11111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select lives_ok(
  $$ insert into app.shops (id, organization_id, name, marketplace, status)
     values ('a3000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000000', 'Shop A3', 'etsy', 'pending') $$,
  'owner A can create a shop with an aal2 session'
);
update app.memberships set role = 'viewer' where id = 'ae000000-0000-0000-0000-000000000002';
reset role;
select is((select role from app.memberships where id = 'ae000000-0000-0000-0000-000000000002'), 'viewer',
  'owner A can change a role with an aal2 session');

-- Clients outside the API get nothing ----------------------------------------------------------

set local role anon;
select throws_ok($$ select * from app.organizations $$, '42501', null, 'anon cannot read app tables');
reset role;

select set_config('request.jwt.claims', '{"sub":"11111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select throws_ok($$ select private.user_org_ids() $$, '42501', null,
  'authenticated cannot call the private helpers directly');
reset role;

select * from finish();
rollback;
