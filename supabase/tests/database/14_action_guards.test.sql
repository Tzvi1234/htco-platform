-- The Action Gateway's guards (slice 5.15): rate windows, circuit breakers and concurrency slots
-- live in the jobs schema, reachable by the worker role only; deferred executions keep retry_at.
begin;
create extension if not exists pgtap with schema extensions;
select plan(12);

select has_column('app', 'action_executions', 'retry_at', 'a deferred execution keeps retry_at');

select ok((select relrowsecurity from pg_class where oid = 'jobs.rate_windows'::regclass),
  'rate_windows has RLS enabled');
select ok((select relrowsecurity from pg_class where oid = 'jobs.circuit_breakers'::regclass),
  'circuit_breakers has RLS enabled');
select ok((select relrowsecurity from pg_class where oid = 'jobs.concurrency_slots'::regclass),
  'concurrency_slots has RLS enabled');

select ok(not has_table_privilege('authenticated', 'jobs.rate_windows', 'select'),
  'users never read rate windows');
select ok(not has_table_privilege('anon', 'jobs.circuit_breakers', 'select'),
  'anon never reads breakers');
select ok(not has_table_privilege('authenticated', 'jobs.concurrency_slots', 'insert'),
  'users never take slots');
select ok(has_table_privilege('htco_worker', 'jobs.rate_windows', 'insert, update, delete'),
  'the worker spends the budget');
select ok(has_table_privilege('htco_worker', 'jobs.concurrency_slots', 'insert, update, delete'),
  'the worker takes and releases slots');

select throws_ok(
  $$ insert into jobs.rate_windows (key, scope, starts_at, used)
     values ('etsy:app', 'second', now(), -1) $$,
  '23514', null, 'a window never has negative use'
);
select throws_ok(
  $$ insert into jobs.circuit_breakers (key, state) values ('etsy:api', 'broken') $$,
  '23514', null, 'a breaker is closed, open or half-open'
);
select throws_ok(
  $$ insert into jobs.concurrency_slots (key, slot, holder, lease_until)
     values ('shop:x:writes', -1, gen_random_uuid(), now()) $$,
  '23514', null, 'slot numbers start at 0'
);

select * from finish();
rollback;
