-- Job queue and worker role (slice 5.1): no client role sees the queue; one running job per lock
-- key; the worker role reaches one organization's shop rows at a time and nothing else.
begin;
create extension if not exists pgtap with schema extensions;
-- pgTAP lives in `extensions`; the worker role needs to see it while the test runs as that role
-- (rolled back with the test).
grant usage on schema extensions to htco_worker;
select plan(21);

insert into app.organizations (id, name) values
  ('8a000000-0000-0000-0000-000000000000', 'Organization E'),
  ('8b000000-0000-0000-0000-000000000000', 'Organization F');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('8c000000-0000-0000-0000-000000000001', '8a000000-0000-0000-0000-000000000000', 'Shop E', 'etsy', 'active'),
  ('8d000000-0000-0000-0000-000000000001', '8b000000-0000-0000-0000-000000000000', 'Shop F', 'etsy', 'active');
insert into app.shop_integrations
  (id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, key_id, wrapped_data_key,
   access_token_ciphertext, refresh_token_ciphertext, scopes, access_expires_at,
   refresh_expires_at, status)
values
  ('8c100000-0000-0000-0000-000000000001', '8a000000-0000-0000-0000-000000000000',
   '8c000000-0000-0000-0000-000000000001', 'etsy', 801, 901, 'key-a', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected'),
  ('8d100000-0000-0000-0000-000000000001', '8b000000-0000-0000-0000-000000000000',
   '8d000000-0000-0000-0000-000000000001', 'etsy', 802, 902, 'key-a', '\x01aa', '\x01bb', '\x01cc',
   '{shops_r}', now() + interval '1 hour', now() + interval '90 days', 'connected');

select has_schema('jobs');
select has_table('jobs', 'jobs', 'jobs.jobs exists');
select has_table('jobs', 'worker_heartbeats', 'jobs.worker_heartbeats exists');
select ok(
  (select relrowsecurity from pg_class where oid = 'jobs.jobs'::regclass)
  and (select relrowsecurity from pg_class where oid = 'jobs.worker_heartbeats'::regclass),
  'the queue tables have RLS enabled'
);
select ok(not has_schema_privilege('anon', 'jobs', 'usage'), 'anon cannot use the jobs schema');
select ok(not has_schema_privilege('authenticated', 'jobs', 'usage'),
  'authenticated cannot use the jobs schema');
select ok(not has_schema_privilege('service_role', 'jobs', 'usage'),
  'service_role cannot use the jobs schema');
select ok(not has_table_privilege('authenticated', 'jobs.jobs', 'select'),
  'authenticated cannot read jobs');
select ok(
  (select not rolcanlogin and not rolsuper and not rolbypassrls from pg_roles
   where rolname = 'htco_worker'),
  'htco_worker cannot log in, is no superuser and does not bypass RLS'
);

-- Constraints.
insert into jobs.jobs
  (id, kind, idempotency_key, max_attempts, initial_backoff_ms, max_backoff_ms, run_at)
values ('8e000000-0000-0000-0000-000000000001', 'test.lock', 'a', 3, 1000, 2000, now());
select throws_ok(
  $$ insert into jobs.jobs
       (id, kind, idempotency_key, max_attempts, initial_backoff_ms, max_backoff_ms, run_at)
     values ('8e000000-0000-0000-0000-000000000002', 'test.lock', 'a', 3, 1000, 2000, now()) $$,
  '23505', null, 'one job per kind and idempotency key'
);
select throws_ok(
  $$ insert into jobs.jobs
       (id, kind, idempotency_key, max_attempts, initial_backoff_ms, max_backoff_ms, run_at)
     values ('8e000000-0000-0000-0000-000000000003', 'NoDot', 'b', 3, 1000, 2000, now()) $$,
  '23514', null, 'job kinds are <area>.<action>'
);
select throws_ok(
  $$ insert into jobs.jobs
       (id, kind, idempotency_key, state, max_attempts, initial_backoff_ms, max_backoff_ms,
        run_at)
     values ('8e000000-0000-0000-0000-000000000004', 'test.lock', 'c', 'running', 3, 1000, 2000,
             now()) $$,
  '23514', null, 'a running job needs a holder and a lease'
);
select throws_ok(
  $$ insert into jobs.jobs
       (id, kind, idempotency_key, shop_id, max_attempts, initial_backoff_ms, max_backoff_ms,
        run_at)
     values ('8e000000-0000-0000-0000-000000000005', 'test.lock', 'd',
             '8c000000-0000-0000-0000-000000000001', 3, 1000, 2000, now()) $$,
  '23514', null, 'a shop''s job names its organization'
);
select throws_ok(
  $$ insert into jobs.jobs
       (id, kind, idempotency_key, organization_id, shop_id, max_attempts, initial_backoff_ms,
        max_backoff_ms, run_at)
     values ('8e000000-0000-0000-0000-000000000006', 'test.lock', 'e',
             '8b000000-0000-0000-0000-000000000000', '8c000000-0000-0000-0000-000000000001', 3,
             1000, 2000, now()) $$,
  '23503', null, 'a job''s shop belongs to the job''s organization'
);
insert into jobs.jobs
  (id, kind, idempotency_key, lock_key, state, leased_by, lease_expires_at, max_attempts,
   initial_backoff_ms, max_backoff_ms, run_at)
values ('8e000000-0000-0000-0000-000000000007', 'test.lock', 'f', 'shop:x', 'running', 'w1',
        now() + interval '1 minute', 3, 1000, 2000, now());
select throws_ok(
  $$ insert into jobs.jobs
       (id, kind, idempotency_key, lock_key, state, leased_by, lease_expires_at, max_attempts,
        initial_backoff_ms, max_backoff_ms, run_at)
     values ('8e000000-0000-0000-0000-000000000008', 'test.lock', 'g', 'shop:x', 'running', 'w2',
             now() + interval '1 minute', 3, 1000, 2000, now()) $$,
  '23505', null, 'AC-5.1.2: one running job per lock key'
);

-- The worker role: the queue, and one organization at a time.
set local role htco_worker;
select is(
  (select count(*)::int from jobs.jobs where kind = 'test.lock'), 2,
  'the worker reads the queue'
);
select is_empty(
  $$ select id from app.shop_integrations $$,
  'outside a job the worker sees no tenant row'
);
select set_config('htco.organization_id', '8a000000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select shop_id from app.shop_integrations $$,
  $$ values ('8c000000-0000-0000-0000-000000000001'::uuid) $$,
  'inside a job the worker sees its organization''s integration only'
);
select results_eq(
  $$ select organization_id, shop_id from jobs.connected_shops()
     where shop_id in ('8c000000-0000-0000-0000-000000000001', '8d000000-0000-0000-0000-000000000001')
     order by shop_id $$,
  $$ values ('8a000000-0000-0000-0000-000000000000'::uuid,
             '8c000000-0000-0000-0000-000000000001'::uuid),
            ('8b000000-0000-0000-0000-000000000000'::uuid,
             '8d000000-0000-0000-0000-000000000001'::uuid) $$,
  'the fan-out lists connected shops by ID across organizations'
);
select throws_ok(
  $$ select count(*) from app.memberships $$,
  '42501', null, 'the worker cannot read memberships'
);
select throws_ok(
  $$ select count(*) from private.web_sessions $$,
  '42501', null, 'the worker cannot read web sessions'
);
reset role;

select * from finish();
rollback;
