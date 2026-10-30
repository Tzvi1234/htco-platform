-- One resumer per action execution (Codex post-merge review, owner gate v4): the compare-and-set
-- on `generation` elects one winner, a held lease refuses a second, and users never see either.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(6);

insert into app.organizations (id, name) values
  ('46a00000-0000-0000-0000-000000000000', 'Organization R');
insert into app.action_executions
  (id, organization_id, action_name, action_version, idempotency_key, request_hash, state,
   outcome, actor_kind, actor_id, correlation_id)
values
  ('46b00000-0000-0000-0000-000000000001', '46a00000-0000-0000-0000-000000000000',
   'etsy.receipts.read', 1, 'k-46', repeat('a', 64), 'deferred', 'deferred', 'automation',
   'etsy-order-intake', 'c-46');

select is((select generation from app.action_executions
           where id = '46b00000-0000-0000-0000-000000000001'), 0, 'a new row starts at generation 0');
select ok(not has_column_privilege('authenticated', 'app.action_executions', 'resuming_until', 'select'),
  'users never read the resumption lease');

set local role htco_worker;
select set_config('htco.organization_id', '46a00000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ update app.action_executions set generation = generation + 1,
       resuming_until = now() + interval '1 minute'
     where id = '46b00000-0000-0000-0000-000000000001' and state = 'deferred' and generation = 0
       and (resuming_until is null or resuming_until <= now())
     returning generation $$,
  $$ values (1) $$,
  'the first resumption wins and raises the generation'
);
select is_empty(
  $$ update app.action_executions set generation = generation + 1,
       resuming_until = now() + interval '1 minute'
     where id = '46b00000-0000-0000-0000-000000000001' and state = 'deferred' and generation = 0
       and (resuming_until is null or resuming_until <= now())
     returning generation $$,
  'a second resumption at the old generation loses'
);
select is_empty(
  $$ update app.action_executions set generation = generation + 1
     where id = '46b00000-0000-0000-0000-000000000001' and state = 'deferred' and generation = 1
       and (resuming_until is null or resuming_until <= now())
     returning generation $$,
  'and so does one at the new generation while the lease holds'
);
reset role;

select throws_ok(
  $$ update app.action_executions set generation = -1
     where id = '46b00000-0000-0000-0000-000000000001' $$,
  '23514', null, 'the generation never goes below 0'
);

select * from finish();
rollback;
