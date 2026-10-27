-- Browser dispatch, the browser worker's side (slice 8A.2c, AC-8A.2.18, 2.21, 2.23 to 2.28):
-- the lease token, claiming `browser.*` jobs only, starting and finishing a run through the
-- leased job and the run token, one live run per profile, fail-closed gates, and the profile
-- archives only through a leased job.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_browser, htco_worker, authenticated;
select plan(46);

-- A scratch table for the tokens the functions hand out (the tests' own, not the schema's).
create temp table t (k text primary key, v text);
grant select, insert, update on t to htco_browser;

insert into app.organizations (id, name) values
  ('d4a00000-0000-0000-0000-000000000000', 'Organization Dispatch A'),
  ('d4b00000-0000-0000-0000-000000000000', 'Organization Dispatch B');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('d4a10000-0000-0000-0000-000000000001', 'd4a00000-0000-0000-0000-000000000000', 'Shop DA', 'etsy', 'active'),
  ('d4b10000-0000-0000-0000-000000000001', 'd4b00000-0000-0000-0000-000000000000', 'Shop DB', 'etsy', 'active');
insert into app.shop_browser_environments (id, organization_id, shop_id, status) values
  ('d4a20000-0000-0000-0000-000000000001', 'd4a00000-0000-0000-0000-000000000000',
   'd4a10000-0000-0000-0000-000000000001', 'provisioning'),
  ('d4b20000-0000-0000-0000-000000000001', 'd4b00000-0000-0000-0000-000000000000',
   'd4b10000-0000-0000-0000-000000000001', 'provisioning');
update app.shop_browser_environments set status = 'ready';
insert into app.browser_profile_archives (environment_id, organization_id, shop_id, key_id, archive, sealed_at) values
  ('d4a20000-0000-0000-0000-000000000001', 'd4a00000-0000-0000-0000-000000000000',
   'd4a10000-0000-0000-0000-000000000001', 'browser-1', '\x01aa', now()),
  ('d4b20000-0000-0000-0000-000000000001', 'd4b00000-0000-0000-0000-000000000000',
   'd4b10000-0000-0000-0000-000000000001', 'browser-1', '\x01bb', now());

-- Four executions of shop A, each dispatched with its binding (1, 2: reads without approval;
-- 3: needs an approval; 4: a read for the unapproved-read path), and one of shop B.
insert into app.action_executions
  (id, organization_id, shop_id, action_name, action_version, idempotency_key, request_hash,
   state, channel, actor_kind, actor_id, correlation_id)
select ('d4a3000' || n || '-0000-0000-0000-000000000000')::uuid, 'd4a00000-0000-0000-0000-000000000000',
       'd4a10000-0000-0000-0000-000000000001', 'autova.order.read', 1, 'key-' || n, repeat('a', 64),
       'dispatched', 'browser', 'system', 'test', 'corr-' || n
from generate_series(1, 4) n;
insert into app.action_executions
  (id, organization_id, shop_id, action_name, action_version, idempotency_key, request_hash,
   state, channel, actor_kind, actor_id, correlation_id)
values ('d4b30001-0000-0000-0000-000000000000', 'd4b00000-0000-0000-0000-000000000000',
        'd4b10000-0000-0000-0000-000000000001', 'autova.order.read', 1, 'key-b', repeat('a', 64),
        'dispatched', 'browser', 'system', 'test', 'corr-b');
insert into app.browser_run_bindings
  (action_execution_id, attempt, organization_id, shop_id, environment_id, action_name, recipe_id,
   recipe_version, parameters, request_hash, needs_approval, is_write, deadline)
select ('d4a3000' || n || '-0000-0000-0000-000000000000')::uuid, 1, 'd4a00000-0000-0000-0000-000000000000',
       'd4a10000-0000-0000-0000-000000000001', 'd4a20000-0000-0000-0000-000000000001',
       'autova.order.read', 'autova.order_status', 1, '{"order": 1}', repeat('a', 64), n = 3, false,
       now() + interval '1 hour'
from generate_series(1, 4) n;
insert into app.browser_run_bindings
  (action_execution_id, attempt, organization_id, shop_id, environment_id, action_name, recipe_id,
   recipe_version, request_hash, needs_approval, is_write, deadline)
values ('d4b30001-0000-0000-0000-000000000000', 1, 'd4b00000-0000-0000-0000-000000000000',
        'd4b10000-0000-0000-0000-000000000001', 'd4b20000-0000-0000-0000-000000000001',
        'autova.order.read', 'autova.order_status', 1, repeat('a', 64), false, false,
        now() + interval '1 hour');

-- Enqueue the job for execution n (a locator only), due now.
create function pg_temp.enqueue(n int) returns void language sql as $$
  insert into jobs.jobs (id, kind, idempotency_key, organization_id, shop_id, payload, max_attempts,
                         initial_backoff_ms, max_backoff_ms, run_at)
  values (('d4a4000' || n || '-0000-0000-0000-000000000000')::uuid, 'browser.run', 'browser-run-' || n,
          'd4a00000-0000-0000-0000-000000000000', 'd4a10000-0000-0000-0000-000000000001',
          jsonb_build_object('action_execution_id', 'd4a3000' || n || '-0000-0000-0000-000000000000', 'attempt', 1),
          3, 1000, 1000, now() - interval '1 second');
$$;
grant execute on function pg_temp.enqueue(int) to htco_browser;

-- Structure, grants and hardening.
select ok(
  not has_table_privilege('htco_browser', 'jobs.jobs', 'select, insert, update, delete')
  and not has_table_privilege('htco_browser', 'app.browser_profile_archives', 'select, insert, update, delete')
  and not has_table_privilege('htco_browser', 'app.browser_run_outcomes', 'select, insert, update, delete')
  and not has_table_privilege('htco_browser', 'app.browser_run_bindings', 'select, insert, update, delete')
  and not has_table_privilege('htco_browser', 'app.action_executions', 'select, insert, update, delete'),
  'AC-8A.2.18/2.23: the browser role has no right on the queue, archives, runs, bindings or executions'
);
select ok(
  not has_column_privilege('htco_browser', 'app.shop_browser_environments', 'profile_storage_key', 'update'),
  'AC-8A.2.23: the browser role writes its profile record only through the leased functions'
);
select ok(not has_table_privilege('authenticated', 'app.browser_run_outcomes', 'select'),
  'AC-8A.2.18: authenticated has no access to run rows');
select ok(
  not exists (
    select from pg_proc p join pg_namespace s on s.oid = p.pronamespace
    where s.nspname = 'browser'
      and (not p.prosecdef or not ('search_path=""' = any (p.proconfig))
           or has_function_privilege('authenticated', p.oid, 'execute')
           or has_function_privilege('htco_worker', p.oid, 'execute')
           or has_function_privilege('anon', p.oid, 'execute')
           or not has_function_privilege('htco_browser', p.oid, 'execute'))
  ) and (select count(*) from pg_proc p join pg_namespace s on s.oid = p.pronamespace
         where s.nspname = 'browser') = 10,
  'AC-8A.2.18: every entry point is security definer, pins search_path and is the browser role''s only'
);
select ok(not has_schema_privilege('htco_browser', 'private', 'usage')
  and not has_schema_privilege('htco_worker', 'browser', 'usage'),
  'the browser role cannot reach private helpers, and the agent-worker cannot reach the browser schema');
select ok(
  'dispatched' = any (select unnest(regexp_matches(pg_get_constraintdef(oid), '''([a-z_]+)''', 'g'))
                      from pg_constraint where conname = 'action_executions_state_check'),
  'an action can be dispatched'
);

-- The gates start empty and no role writes them.
select is((select count(*)::int from app.browser_run_gates), 0, 'the gates start empty: fail closed');
set local role htco_browser;
select throws_ok($$ insert into app.browser_run_gates (id, organization_id, kind, actor_id, correlation_id)
  values (gen_random_uuid(), 'd4a00000-0000-0000-0000-000000000000', 'kill_switch', 'x', 'c') $$,
  '42501', null, 'the browser role cannot write a gate');
reset role;
set local role htco_worker;
select throws_ok($$ insert into app.browser_run_gates (id, organization_id, kind, actor_id, correlation_id)
  values (gen_random_uuid(), 'd4a00000-0000-0000-0000-000000000000', 'kill_switch', 'x', 'c') $$,
  '42501', null, 'the agent-worker cannot write a gate');
reset role;
set local role authenticated;
select throws_ok($$ delete from app.browser_run_gates $$, '42501', null, 'a signed-in user cannot write a gate');
reset role;

-- Claiming: browser jobs only, with a token stored as its hash.
insert into jobs.jobs (id, kind, idempotency_key, organization_id, max_attempts, initial_backoff_ms,
                       max_backoff_ms, run_at, priority)
values (gen_random_uuid(), 'orders.sync', 'other', 'd4a00000-0000-0000-0000-000000000000', 3, 1000, 1000,
        now() - interval '1 hour', 10);
select pg_temp.enqueue(1);
set local role htco_browser;
select set_config('htco.organization_id', 'd4b00000-0000-0000-0000-000000000000', true);
insert into t select 'job1', lease_token from browser.claim_job('worker-1', 60);
reset role;
select is((select state from jobs.jobs where idempotency_key = 'browser-run-1'), 'running',
  'AC-8A.2.18: the browser role claims a browser job');
select is((select state from jobs.jobs where idempotency_key = 'other'), 'queued',
  'and never another kind, even one due earlier with a higher priority');
select ok(
  (select lease_token_hash = sha256(convert_to((select v from t where k = 'job1'), 'UTF8'))
   from jobs.jobs where idempotency_key = 'browser-run-1')
  and char_length((select v from t where k = 'job1')) = 64,
  'the token is unguessable and stored only as its SHA-256'
);

-- Starting: under the profile lock only (AC-8A.2.28), with the leased token only.
set local role htco_browser;
select throws_ok(
  $$ select * from browser.start_run('d4a40001-0000-0000-0000-000000000000', (select v from t where k = 'job1')) $$,
  '42501', null, 'AC-8A.2.28: a start without the profile lock is refused'
);
reset role;
select pg_advisory_lock(hashtextextended('htco.browser_profile:d4a20000-0000-0000-0000-000000000001', 0));
set local role htco_browser;
select throws_ok(
  $$ select * from browser.start_run('d4a40001-0000-0000-0000-000000000000', 'forged') $$,
  '42501', null, 'AC-8A.2.18: a forged lease token is refused'
);
select results_eq(
  $$ select outcome, refusal from browser.start_run('d4a40001-0000-0000-0000-000000000000', (select v from t where k = 'job1')) $$,
  $$ values ('refused'::text, 'flag_off'::text) $$,
  'AC-8A.2.26: with no flag row the run is refused (fail closed)'
);
reset role;
select is(
  (select status || ':' || error_code from app.browser_run_outcomes
   where action_execution_id = 'd4a30001-0000-0000-0000-000000000000'),
  'refused:flag_off', 'the refusal is a final row with its code'
);
select is(
  (select payload ->> 'status' from jobs.outbox
   where idempotency_key = 'browser.run_finished.v1:d4a30001-0000-0000-0000-000000000000:1'),
  'refused', 'and its event is published once'
);

-- Switch the flag on (the Owner's function comes later; here as the table owner).
insert into app.browser_run_gates (id, organization_id, shop_id, action_name, kind, actor_id, correlation_id)
values (gen_random_uuid(), 'd4a00000-0000-0000-0000-000000000000', 'd4a10000-0000-0000-0000-000000000001',
        'autova.order.read', 'flag_on', 'owner', 'corr-flag');
select pg_temp.enqueue(2);
set local role htco_browser;
insert into t select 'job2', lease_token from browser.claim_job('worker-1', 60);
insert into t select 'run2', run_token
  from browser.start_run('d4a40002-0000-0000-0000-000000000000', (select v from t where k = 'job2'));
reset role;
select is(
  (select status from app.browser_run_outcomes where action_execution_id = 'd4a30002-0000-0000-0000-000000000000'),
  'started', 'AC-8A.2.19: the started marker is committed, the organization setting notwithstanding'
);
select ok(
  (select char_length(v) = 64 from t where k = 'run2')
  and (select run_token_hash = sha256(convert_to((select v from t where k = 'run2'), 'UTF8'))
       from app.browser_run_outcomes where action_execution_id = 'd4a30002-0000-0000-0000-000000000000'),
  'AC-8A.2.24: the run token is stored only as its hash'
);
set local role htco_browser;
select is_empty(
  $$ select * from browser.start_run('d4a40002-0000-0000-0000-000000000000', (select v from t where k = 'job2')) $$,
  'AC-8A.2.21: starting the same attempt again returns nothing, not even the binding'
);
reset role;

-- One live run per profile (AC-8A.2.25).
select pg_temp.enqueue(4);
set local role htco_browser;
insert into t select 'job4', lease_token from browser.claim_job('worker-2', 60);
select throws_ok(
  $$ select * from browser.start_run('d4a40004-0000-0000-0000-000000000000', (select v from t where k = 'job4')) $$,
  '55P03', null, 'AC-8A.2.25: a second run of the same profile cannot start while one is live'
);
-- The heartbeat renews a live lease only.
select ok(browser.renew_lease('d4a40002-0000-0000-0000-000000000000', (select v from t where k = 'job2'), 120) > now() + interval '100 seconds',
  'AC-8A.2.21: the heartbeat renews the lease');
select throws_ok(
  $$ select browser.renew_lease('d4a40002-0000-0000-0000-000000000000', (select v from t where k = 'job4'), 60) $$,
  '42501', null, 'another job''s token renews nothing'
);
reset role;

-- The lease expires (a stopped heartbeat); the run still records its outcome by its run token.
update jobs.jobs set lease_expires_at = now() - interval '1 second' where idempotency_key = 'browser-run-2';
set local role htco_browser;
select throws_ok(
  $$ select browser.renew_lease('d4a40002-0000-0000-0000-000000000000', (select v from t where k = 'job2'), 60) $$,
  '42501', null, 'an expired lease is not renewed'
);
select is(browser.finish_run('forged', 'succeeded', null, null, null), false,
  'AC-8A.2.21: a forged run token records nothing');
select throws_ok(
  $$ select browser.finish_run((select v from t where k = 'run2'), 'interrupted', null, null, null) $$,
  '22023', null, 'only the watchdog sets interrupted'
);
select is(browser.finish_run((select v from t where k = 'run2'), 'succeeded', null, '{"status": "shipped"}', '[]'),
  true, 'AC-8A.2.21: the outcome is recorded after the job''s lease expired');
select is(browser.finish_run((select v from t where k = 'run2'), 'failed', 'error', null, null), false,
  'a used run token records nothing');
reset role;
select results_eq(
  $$ select status, output ->> 'status', run_token_hash is null from app.browser_run_outcomes
     where action_execution_id = 'd4a30002-0000-0000-0000-000000000000' $$,
  $$ values ('succeeded'::text, 'shipped'::text, true) $$,
  'set once, and the token is gone'
);
select is(
  (select count(*)::int from jobs.outbox
   where idempotency_key = 'browser.run_finished.v1:d4a30002-0000-0000-0000-000000000000:1'),
  1, 'AC-8A.2.16: browser.run_finished.v1 once per execution and attempt'
);
select throws_ok(
  $$ update app.browser_run_outcomes set status = 'failed' where action_execution_id = 'd4a30002-0000-0000-0000-000000000000' $$,
  '23514', null, 'a final outcome never changes'
);

-- An approval-gated run fails closed until Phase 7's approvals table exists.
select pg_temp.enqueue(3);
set local role htco_browser;
insert into t select 'job3', lease_token from browser.claim_job('worker-1', 60);
select results_eq(
  $$ select outcome, refusal from browser.start_run('d4a40003-0000-0000-0000-000000000000', (select v from t where k = 'job3')) $$,
  $$ values ('refused'::text, 'approval_unavailable'::text) $$,
  'AC-8A.2.15: a run that needs an approval is refused with approval_unavailable'
);
reset role;

-- A kill switch refuses a start; a gate change and a start are serialized on the gates table.
insert into app.browser_run_gates (id, organization_id, kind, actor_id, correlation_id)
values (gen_random_uuid(), 'd4a00000-0000-0000-0000-000000000000', 'kill_switch', 'owner', 'corr-kill');
set local role htco_browser;
select results_eq(
  $$ select outcome, refusal from browser.start_run('d4a40004-0000-0000-0000-000000000000', (select v from t where k = 'job4')) $$,
  $$ values ('refused'::text, 'kill_switch'::text) $$,
  'AC-8A.2.26: a kill switch refuses the start'
);
reset role;

-- The unapproved read records its final row with its leased job, once (AC-8A.2.24).
select is(
  (select count(*)::int from app.browser_run_outcomes where action_execution_id = 'd4a30004-0000-0000-0000-000000000000'),
  1, 'the refused attempt keeps its row'
);
set local role htco_browser;
select throws_ok(
  $$ select browser.record_unapproved_read('d4a40003-0000-0000-0000-000000000000', (select v from t where k = 'job3'), 'succeeded', null, null, null) $$,
  '42501', null, 'an approval-gated run cannot record itself as an unapproved read'
);
select is(
  browser.record_unapproved_read('d4a40004-0000-0000-0000-000000000000', (select v from t where k = 'job4'), 'succeeded', null, null, null),
  false, 'an attempt with a row is never recorded again'
);

-- Completing the job clears its token.
select lives_ok(
  $$ select browser.complete_job('d4a40004-0000-0000-0000-000000000000', (select v from t where k = 'job4')) $$,
  'the browser role completes its job'
);
reset role;
select ok(
  (select state = 'succeeded' and lease_token_hash is null from jobs.jobs where idempotency_key = 'browser-run-4'),
  'a finished job keeps no token'
);

-- The archives, only through a leased job (AC-8A.2.23).
set local role htco_browser;
select set_config('htco.organization_id', 'd4b00000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select environment_id, generation from browser.open_profile('d4a40003-0000-0000-0000-000000000000', (select v from t where k = 'job3')) $$,
  $$ values ('d4a20000-0000-0000-0000-000000000001'::uuid, 1::bigint) $$,
  'AC-8A.2.23: a leased job reaches only its own environment''s archive, whatever the setting says'
);
select throws_ok(
  $$ select * from browser.open_profile('d4a40004-0000-0000-0000-000000000000', (select v from t where k = 'job4')) $$,
  '42501', null, 'a finished job opens no archive'
);
select throws_ok(
  $$ select * from browser.open_profile('d4a40003-0000-0000-0000-000000000000', 'forged') $$,
  '42501', null, 'a forged token opens no archive'
);
select is(
  browser.save_profile('d4a40003-0000-0000-0000-000000000000', (select v from t where k = 'job3'),
                       'browser-1', '\x01cc', now(), 'browser-profiles/a', 1),
  2::bigint, 'the write-back is a compare-and-set: generation 1 becomes 2'
);
select throws_ok(
  $$ select browser.save_profile('d4a40003-0000-0000-0000-000000000000', (select v from t where k = 'job3'),
                                 'browser-1', '\x01dd', now(), 'browser-profiles/a', 1) $$,
  '40001', null, 'a stale write-back is refused'
);
reset role;

update jobs.jobs set lease_expires_at = now() - interval '1 second' where idempotency_key = 'browser-run-3';
set local role htco_browser;
select throws_ok(
  $$ select * from browser.open_profile('d4a40003-0000-0000-0000-000000000000', (select v from t where k = 'job3')) $$,
  '42501', null, 'an expired lease opens no archive'
);
reset role;
select results_eq(
  $$ select archive, generation from app.browser_profile_archives order by environment_id $$,
  $$ values ('\x01cc'::bytea, 2::bigint), ('\x01bb'::bytea, 1::bigint) $$,
  'only the one compare-and-set write landed; the other organization''s archive is untouched'
);

select * from finish();
rollback;
