-- Identity and access: RLS, tenant isolation, shop scope, append-only audit, MFA for writes
-- (phase 2, slice 2.2: AC-2.2.1 to AC-2.2.5).
begin;
create extension if not exists pgtap with schema extensions;

select plan(27);

-- Two organizations: A with shops A1 and A2, B with shop B1.
create temporary table ids on commit drop as
select
  gen_random_uuid() as org_a, gen_random_uuid() as org_b,
  gen_random_uuid() as shop_a1, gen_random_uuid() as shop_a2, gen_random_uuid() as shop_b1,
  tests.create_user('owner-a@example.test') as owner_a,
  tests.create_user('support-a@example.test') as support_a,
  tests.create_user('owner-b@example.test') as owner_b,
  gen_random_uuid() as membership_support_a;
grant select on ids to authenticated;

insert into app.organizations (id, name)
select org_a, 'Org A' from ids union all select org_b, 'Org B' from ids;
insert into app.shops (id, organization_id, name, marketplace)
select shop_a1, org_a, 'Shop A1', 'etsy' from ids
union all select shop_a2, org_a, 'Shop A2', 'etsy' from ids
union all select shop_b1, org_b, 'Shop B1', 'etsy' from ids;
insert into app.memberships (id, organization_id, user_id, role)
select gen_random_uuid(), org_a, owner_a, 'owner' from ids
union all select membership_support_a, org_a, support_a, 'support' from ids
union all select gen_random_uuid(), org_b, owner_b, 'owner' from ids;
insert into app.shop_access (id, organization_id, membership_id, shop_id)
select gen_random_uuid(), org_a, membership_support_a, shop_a1 from ids;
insert into app.user_profiles (user_id, display_name)
select owner_a, 'Owner A' from ids union all select owner_b, 'Owner B' from ids;
insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type)
select gen_random_uuid(), org_a, 'system', 'test', 'test.seeded', 'organization' from ids
union all
select gen_random_uuid(), org_b, 'system', 'test', 'test.seeded', 'organization' from ids;

-- AC-2.2.1: every table in app and audit has row-level security.
select is(
  (select count(*)::int from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname in ('app', 'audit') and c.relkind in ('r', 'p') and not c.relrowsecurity),
  0,
  'AC-2.2.1: every table in app and audit has RLS enabled'
);

-- AC-2.2.2: a user of organization A sees nothing of organization B, in every table.
select tests.authenticate_as((select owner_a from ids));
select is((select count(*)::int from app.organizations where id = (select org_b from ids)), 0,
  'AC-2.2.2: organizations of B are invisible to A');
select is((select count(*)::int from app.memberships where organization_id = (select org_b from ids)), 0,
  'AC-2.2.2: memberships of B are invisible to A');
select is((select count(*)::int from app.shops where organization_id = (select org_b from ids)), 0,
  'AC-2.2.2: shops of B are invisible to A');
select is((select count(*)::int from app.shop_access where organization_id = (select org_b from ids)), 0,
  'AC-2.2.2: shop grants of B are invisible to A');
select is((select count(*)::int from app.user_profiles where user_id = (select owner_b from ids)), 0,
  'AC-2.2.2: profiles of B are invisible to A');
select is((select count(*)::int from audit.audit_log where organization_id = (select org_b from ids)), 0,
  'AC-2.2.2: audit records of B are invisible to A');
-- …and the same user does see its own organization, so the zeros above are not vacuous.
select is((select count(*)::int from app.shops), 2, 'the owner of A sees both shops of A');
select is((select count(*)::int from app.memberships), 2, 'the owner of A sees the members of A');
select is((select count(*)::int from audit.audit_log), 1, 'the owner of A reads the audit log of A');
reset role;

-- AC-2.2.3: a Support user granted shop A1 only cannot read shop A2.
select tests.authenticate_as((select support_a from ids));
select is(
  (select array_agg(id) from app.shops),
  array[(select shop_a1 from ids)],
  'AC-2.2.3: Support sees exactly its granted shop'
);
select is((select count(*)::int from app.shops where id = (select shop_a2 from ids)), 0,
  'AC-2.2.3: Support cannot read a shop it was not granted');
select is((select count(*)::int from audit.audit_log), 0,
  'Support cannot read the audit log');
select throws_ok(
  $$ insert into app.shops (id, organization_id, name, marketplace)
     select gen_random_uuid(), org_a, 'Shop X', 'etsy' from ids $$,
  '42501', null, 'Support cannot create a shop'
);
reset role;

-- AC-2.2.4: nobody can update or delete audit records.
select tests.authenticate_as((select owner_a from ids));
select throws_ok($$ update audit.audit_log set action = 'tampered' $$, '42501', null,
  'AC-2.2.4: the application role cannot update audit records');
select throws_ok($$ delete from audit.audit_log $$, '42501', null,
  'AC-2.2.4: the application role cannot delete audit records');
select lives_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type)
     select gen_random_uuid(), org_a, 'user', owner_a::text, 'test.appended', 'organization'
     from ids $$,
  'the application role can append an audit record for its organization'
);
select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type)
     select gen_random_uuid(), org_b, 'user', owner_a::text, 'test.appended', 'organization'
     from ids $$,
  '42501', null, 'the application role cannot append a record for another organization'
);
reset role;
select throws_ok($$ update audit.audit_log set action = 'tampered' $$, '42501', null,
  'AC-2.2.4: even the table owner cannot update audit records (trigger)');
select throws_ok($$ delete from audit.audit_log $$, '42501', null,
  'AC-2.2.4: even the table owner cannot delete audit records (trigger)');
select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type)
     select gen_random_uuid(), org_a, 'robot', 'x', 'test.appended', 'organization' from ids $$,
  '23514', null, 'an unknown actor kind is rejected'
);

-- AC-2.2.5: owner-administered tables reject writes from a session without MFA (aal1).
select tests.authenticate_as((select owner_a from ids), 'aal1');
select throws_ok(
  $$ insert into app.memberships (id, organization_id, user_id, role)
     select gen_random_uuid(), org_a, owner_b, 'viewer' from ids $$,
  '42501', null, 'AC-2.2.5: adding a member needs an MFA session'
);
select throws_ok(
  $$ insert into app.shops (id, organization_id, name, marketplace)
     select gen_random_uuid(), org_a, 'Shop X', 'etsy' from ids $$,
  '42501', null, 'AC-2.2.5: creating a shop needs an MFA session'
);
select is((select count(*)::int from app.shops), 2,
  'reading still works without MFA');
reset role;

select tests.authenticate_as((select owner_a from ids), 'aal2');
select lives_ok(
  $$ insert into app.memberships (id, organization_id, user_id, role)
     select gen_random_uuid(), org_a, owner_b, 'viewer' from ids $$,
  'with MFA, the owner adds a member'
);
select throws_ok(
  $$ insert into app.shop_access (id, organization_id, membership_id, shop_id)
     select gen_random_uuid(), org_a, membership_support_a, shop_b1 from ids $$,
  '23503', null, 'a grant can never join a membership and a shop of different organizations'
);
reset role;

-- The anonymous role cannot use the business schemas at all.
set local role anon;
select throws_ok($$ select count(*) from app.shops $$, '42501', null,
  'anon cannot read the app schema');
reset role;

select * from finish();
rollback;
