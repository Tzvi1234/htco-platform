-- The audit log accepts exactly the five actor kinds of `ActorKind` (slice 2.5, AC-2.5.3).
begin;
create extension if not exists pgtap with schema extensions;
select plan(7);

select lives_ok(
  format(
    $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
       values (gen_random_uuid(), gen_random_uuid(), %L, 'actor-1', 'test.recorded', 'test', '1') $$,
    kind
  ),
  format('actor kind %s is accepted', kind)
)
from unnest(array['user', 'employee', 'automation', 'advisor', 'system']) as kind;

select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values (gen_random_uuid(), gen_random_uuid(), 'agent', 'actor-1', 'test.recorded', 'test', '1') $$,
  '23514', null, 'the old actor kind "agent" is rejected'
);
select throws_ok(
  $$ insert into audit.audit_log (id, organization_id, actor_kind, actor_id, action, entity_type, entity_id)
     values (gen_random_uuid(), gen_random_uuid(), 'system', '', 'test.recorded', 'test', '1') $$,
  '23514', null, 'an empty actor id is rejected'
);

select * from finish();
rollback;
