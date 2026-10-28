-- A signed-in user appends audit records as themselves only (architecture audit 3.1): the actor
-- must be `user` with the session's own user ID; other kinds and other users' IDs are refused.
-- `service_role` (which bypasses RLS) and the worker roles keep their own paths.
begin;
create extension if not exists pgtap with schema extensions;
select plan(7);

insert into auth.users (id, email) values
  ('3a110000-0000-0000-0000-000000000001', 'owner-audit@example.test'),
  ('3a110000-0000-0000-0000-000000000002', 'member-audit@example.test');
insert into app.organizations (id, name) values
  ('3a000000-0000-0000-0000-000000000000', 'Organization Audit');
insert into app.memberships (id, organization_id, user_id, role) values
  ('3ae00000-0000-0000-0000-000000000001', '3a000000-0000-0000-0000-000000000000',
   '3a110000-0000-0000-0000-000000000001', 'owner'),
  ('3ae00000-0000-0000-0000-000000000002', '3a000000-0000-0000-0000-000000000000',
   '3a110000-0000-0000-0000-000000000002', 'viewer');

select set_config('request.jwt.claims',
  '{"sub":"3a110000-0000-0000-0000-000000000002","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;

select lives_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values (gen_random_uuid(), '3a000000-0000-0000-0000-000000000000', 'user',
             '3a110000-0000-0000-0000-000000000002', 'shop.viewed', 'shop', 's1') $$,
  'a member appends a record as themselves'
);
select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values (gen_random_uuid(), '3a000000-0000-0000-0000-000000000000', 'user',
             '3a110000-0000-0000-0000-000000000001', 'member.removed', 'membership', 'm1') $$,
  '42501', null, 'a member cannot write a record in another user''s name'
);
select throws_ok(
  format(
    $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
       values (gen_random_uuid(), '3a000000-0000-0000-0000-000000000000', %L,
               '3a110000-0000-0000-0000-000000000002', 'test.forged', 'test', '1') $$,
    kind
  ),
  '42501', null, format('a member cannot write a record as actor kind %s', kind)
)
from unnest(array['system', 'automation', 'employee', 'advisor']) as kind;
reset role;

set local role service_role;
select lives_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values (gen_random_uuid(), '3a000000-0000-0000-0000-000000000000', 'system', 'bootstrap',
             'test.recorded', 'test', '1') $$,
  'service_role still appends system records'
);
reset role;

select * from finish();
rollback;
