-- The audit view (AC-3.9.1, `GET /v1/audit-log`): it reads as the caller, so the table's policy
-- decides the rows. Owner and Admin read their organization's records, the shop filter's JSON
-- lookups work under RLS, and every other role, a developer included, reads none.
begin;
create extension if not exists pgtap with schema extensions;
select plan(6);

insert into auth.users (id, email) values
  ('a3333333-0000-0000-0000-000000000001', 'owner-v@example.test'),
  ('a3333333-0000-0000-0000-000000000002', 'admin-v@example.test'),
  ('a3333333-0000-0000-0000-000000000003', 'dev-v@example.test'),
  ('a3333333-0000-0000-0000-000000000004', 'viewer-v@example.test'),
  ('a4444444-0000-0000-0000-000000000001', 'owner-w@example.test');
insert into app.organizations (id, name) values
  ('ba000000-0000-0000-0000-000000000000', 'Organization V'),
  ('bb000000-0000-0000-0000-000000000000', 'Organization W');
insert into app.memberships (id, organization_id, user_id, role) values
  ('be000000-0000-0000-0000-000000000001', 'ba000000-0000-0000-0000-000000000000', 'a3333333-0000-0000-0000-000000000001', 'owner'),
  ('be000000-0000-0000-0000-000000000002', 'ba000000-0000-0000-0000-000000000000', 'a3333333-0000-0000-0000-000000000002', 'admin'),
  ('be000000-0000-0000-0000-000000000003', 'ba000000-0000-0000-0000-000000000000', 'a3333333-0000-0000-0000-000000000003', 'developer'),
  ('be000000-0000-0000-0000-000000000004', 'ba000000-0000-0000-0000-000000000000', 'a3333333-0000-0000-0000-000000000004', 'viewer'),
  ('bf000000-0000-0000-0000-000000000001', 'bb000000-0000-0000-0000-000000000000', 'a4444444-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('bc000000-0000-0000-0000-000000000001', 'ba000000-0000-0000-0000-000000000000', 'Shop V', 'etsy', 'active');
insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id, after) values
  ('bd000000-0000-7000-8000-000000000001', 'ba000000-0000-0000-0000-000000000000', 'user',
   'a3333333-0000-0000-0000-000000000001', 'shop.connected', 'shop', 'bc000000-0000-0000-0000-000000000001', null),
  ('bd000000-0000-7000-8000-000000000002', 'ba000000-0000-0000-0000-000000000000', 'automation',
   'etsy-order-intake', 'order.synced', 'order', '51', '{"shop_id": "bc000000-0000-0000-0000-000000000001"}'),
  ('bd000000-0000-7000-8000-000000000003', 'ba000000-0000-0000-0000-000000000000', 'user',
   'a3333333-0000-0000-0000-000000000001', 'membership.role_changed', 'membership', 'be000000-0000-0000-0000-000000000004', null),
  ('bd000000-0000-7000-8000-000000000004', 'bb000000-0000-0000-0000-000000000000', 'user',
   'a4444444-0000-0000-0000-000000000001', 'shop.connected', 'shop', 'bc000000-0000-0000-0000-000000000001', null);

-- The admin --------------------------------------------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"a3333333-0000-0000-0000-000000000002","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is(
  (select count(*)::integer from audit.audit_log where organization_id = 'ba000000-0000-0000-0000-000000000000'),
  3, 'an admin reads the organization''s records'
);
select results_eq(
  $$ select id from audit.audit_log
     where (entity_type = 'shop' and entity_id = 'bc000000-0000-0000-0000-000000000001')
        or after ->> 'shop_id' = 'bc000000-0000-0000-0000-000000000001'
     order by id desc $$,
  $$ values ('bd000000-0000-7000-8000-000000000002'::uuid), ('bd000000-0000-7000-8000-000000000001'::uuid) $$,
  'the shop filter finds the shop''s own record and the one naming it, never another organization''s'
);
select is(
  (select count(*)::integer from audit.audit_log where starts_with(action, 'membership.')),
  1, 'an action family is a prefix'
);
reset role;

-- Every other role -------------------------------------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"a3333333-0000-0000-0000-000000000003","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*)::integer from audit.audit_log), 0, 'a developer reads no audit record');
reset role;
select set_config('request.jwt.claims', '{"sub":"a3333333-0000-0000-0000-000000000004","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::integer from audit.audit_log), 0, 'a viewer reads no audit record');
reset role;
select set_config('request.jwt.claims', '{"sub":"a4444444-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::integer from audit.audit_log), 1, 'another organization''s owner reads only theirs');
reset role;

select * from finish();
rollback;
