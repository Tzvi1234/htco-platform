-- Local seed (`just seed`, also applied by `just db-reset`). Synthetic data only, never used
-- outside the local stack: two organizations so cross-tenant checks always have a target, their
-- shops, and one user per role in organization 1 (TESTING_STRATEGY §6).
-- Users have no password yet; sign-in for local users arrives with slice 2.3.

insert into auth.users (
  id, instance_id, aud, role, email, raw_app_meta_data, raw_user_meta_data, created_at, updated_at
)
select
  id::uuid, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', email,
  '{}'::jsonb, '{}'::jsonb, now(), now()
from (values
  ('00000000-0000-7000-8000-000000000101', 'owner@org1.example.test'),
  ('00000000-0000-7000-8000-000000000102', 'admin@org1.example.test'),
  ('00000000-0000-7000-8000-000000000103', 'operations@org1.example.test'),
  ('00000000-0000-7000-8000-000000000104', 'finance@org1.example.test'),
  ('00000000-0000-7000-8000-000000000105', 'support@org1.example.test'),
  ('00000000-0000-7000-8000-000000000106', 'developer@org1.example.test'),
  ('00000000-0000-7000-8000-000000000107', 'viewer@org1.example.test'),
  ('00000000-0000-7000-8000-000000000201', 'owner@org2.example.test')
) as seed_users (id, email);

insert into app.user_profiles (user_id, display_name)
select id, initcap(split_part(email, '@', 1)) || ' (seed)' from auth.users
where email like '%.example.test';

insert into app.organizations (id, name) values
  ('00000000-0000-7000-8000-000000000001', 'Seed Organization One'),
  ('00000000-0000-7000-8000-000000000002', 'Seed Organization Two');

insert into app.shops (id, organization_id, name, marketplace) values
  ('00000000-0000-7000-8000-000000001001', '00000000-0000-7000-8000-000000000001', 'Seed Shop 1A', 'etsy'),
  ('00000000-0000-7000-8000-000000001002', '00000000-0000-7000-8000-000000000001', 'Seed Shop 1B', 'etsy'),
  ('00000000-0000-7000-8000-000000002001', '00000000-0000-7000-8000-000000000002', 'Seed Shop 2A', 'etsy');

insert into app.memberships (id, organization_id, user_id, role)
select
  ('00000000-0000-7000-8000-000000003' || right(u.id::text, 3))::uuid,
  case when u.email like '%@org2.%' then '00000000-0000-7000-8000-000000000002'::uuid
       else '00000000-0000-7000-8000-000000000001'::uuid end,
  u.id,
  split_part(u.email, '@', 1)
from auth.users u
where u.email like '%.example.test';

-- Roles below Admin see only granted shops: everyone in organization 1 gets shop 1A.
insert into app.shop_access (id, organization_id, membership_id, shop_id)
select
  ('00000000-0000-7000-8000-000000004' || right(m.id::text, 3))::uuid,
  m.organization_id, m.id, '00000000-0000-7000-8000-000000001001'
from app.memberships m
where m.organization_id = '00000000-0000-7000-8000-000000000001'
  and m.role not in ('owner', 'admin');
