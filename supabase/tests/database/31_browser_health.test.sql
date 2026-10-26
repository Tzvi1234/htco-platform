-- The browser worker's login and its health rights (slice 8A.3).
-- - `htco_browser_login` logs in with no password from any migration, is no superuser, and is
--   a member of `htco_browser` only, without inheriting its rights.
-- - In its own organization, `htco_browser` records health, moves the health and sign-in edges
--   only (AC-8A.3.2, AC-8A.3.3), appends system history rows and writes the status event.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_browser;
select plan(33);

insert into app.organizations (id, name) values
  ('b8c00000-0000-0000-0000-000000000000', 'Organization Health A'),
  ('b8d00000-0000-0000-0000-000000000000', 'Organization Health B');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('b8c10000-0000-0000-0000-000000000001', 'b8c00000-0000-0000-0000-000000000000', 'Shop HA', 'etsy', 'active'),
  ('b8c10000-0000-0000-0000-000000000002', 'b8c00000-0000-0000-0000-000000000000', 'Shop HA2', 'etsy', 'active'),
  ('b8d10000-0000-0000-0000-000000000001', 'b8d00000-0000-0000-0000-000000000000', 'Shop HB', 'etsy', 'active');
insert into app.shop_browser_environments (id, organization_id, shop_id, status) values
  ('b8c20000-0000-0000-0000-000000000001', 'b8c00000-0000-0000-0000-000000000000',
   'b8c10000-0000-0000-0000-000000000001', 'provisioning'),
  ('b8c20000-0000-0000-0000-000000000002', 'b8c00000-0000-0000-0000-000000000000',
   'b8c10000-0000-0000-0000-000000000002', 'provisioning'),
  ('b8d20000-0000-0000-0000-000000000001', 'b8d00000-0000-0000-0000-000000000000',
   'b8d10000-0000-0000-0000-000000000001', 'provisioning');
update app.shop_browser_environments set status = 'ready'
  where id in ('b8c20000-0000-0000-0000-000000000001', 'b8d20000-0000-0000-0000-000000000001');

-- The login.
select ok(
  (select rolcanlogin and not rolsuper and not rolbypassrls and not rolinherit
     and not rolcreaterole and not rolcreatedb and not rolreplication
   from pg_roles where rolname = 'htco_browser_login'),
  'htco_browser_login logs in, is no superuser, does not inherit and does not bypass RLS'
);
-- pg_authid is readable by a superuser only: where the tests run without one, this is skipped.
create function pg_temp.login_has_no_password() returns boolean language plpgsql as $f$
declare
  result boolean;
begin
  execute $q$ select rolpassword is null from pg_authid where rolname = 'htco_browser_login' $q$
    into result;
  return result;
exception when insufficient_privilege then
  return null;
end;
$f$;
select case when pg_temp.login_has_no_password() is null
  then skip('pg_authid needs a superuser', 1)
  else ok(pg_temp.login_has_no_password(), 'no migration sets its password (it is set out of band)')
end;
select is(
  array(select r.rolname::text from pg_auth_members m
        join pg_roles r on r.oid = m.roleid
        join pg_roles u on u.oid = m.member
        where u.rolname = 'htco_browser_login' order by 1),
  array['htco_browser'],
  'it is a member of htco_browser only'
);
select ok(
  (select m.set_option and not m.inherit_option and not m.admin_option
   from pg_auth_members m
   where m.roleid = 'htco_browser'::regrole and m.member = 'htco_browser_login'::regrole),
  'it may switch to htco_browser, without inheriting its rights or granting it on'
);
select ok(not pg_has_role('htco_browser_login', 'htco_worker', 'member')
  and not pg_has_role('htco_browser_login', 'postgres', 'member')
  and not pg_has_role('htco_browser_login', 'service_role', 'member'),
  'it can never become the worker, postgres or the service role');
select is_empty(
  $$ select c.oid::regclass::text from pg_class c join pg_namespace n on n.oid = c.relnamespace
     where c.relkind in ('r', 'v', 'm', 'p', 'f')
       and n.nspname in ('app', 'audit', 'jobs', 'private', 'auth', 'storage', 'public')
       and (has_table_privilege('htco_browser_login', c.oid, 'select, insert, update, delete, truncate, references, trigger')
            or has_any_column_privilege('htco_browser_login', c.oid, 'select, insert, update, references')) $$,
  'before it switches role, the login reaches no table at all'
);

-- The browser role's new rights, and what stays out of reach.
select ok(
  has_column_privilege('htco_browser', 'app.shop_browser_environments', 'status', 'update')
  and has_column_privilege('htco_browser', 'app.shop_browser_environments', 'last_health_ok', 'update'),
  'the browser role records health and moves the status'
);
select ok(not has_table_privilege('htco_browser', 'app.browser_environment_events', 'update, delete'),
  'the history stays append-only for the browser role');
-- Another organization's transition row, for the cross-tenant check.
insert into app.browser_environment_events
  (id, organization_id, shop_id, environment_id, kind, from_status, to_status, actor_kind,
   actor_id, correlation_id, reason, occurred_at)
values ('b8d30000-0000-0000-0000-000000000001', 'b8d00000-0000-0000-0000-000000000000',
        'b8d10000-0000-0000-0000-000000000001', 'b8d20000-0000-0000-0000-000000000001',
        'transition', 'provisioning', 'ready', 'system', 'browser-worker', 'test:b1',
        'ready', now());

select ok(
  not has_table_privilege('htco_browser', 'jobs.jobs', 'select, insert, update, delete')
  and not has_table_privilege('htco_browser', 'jobs.outbox', 'select, insert, update, delete')
  and not has_any_column_privilege('htco_browser', 'jobs.jobs', 'select, insert, update')
  and not has_any_column_privilege('htco_browser', 'jobs.outbox', 'select, insert, update')
  and has_function_privilege('htco_browser', 'app.browser_publish_status_change(uuid)', 'execute')
  and not has_function_privilege('authenticated', 'app.browser_publish_status_change(uuid)', 'execute'),
  'sage on #170: no right on the outbox or the queue; the status event goes through its one writer'
);
select ok(not has_table_privilege('htco_browser', 'jobs.inbox', 'select, insert, update, delete'),
  'it never reaches the inbox');

-- One organization at a time, and only the health and sign-in edges.
set local role htco_browser;
-- Re-plan the status triggers as this role (a plan cached for postgres would hide a missing
-- right, such as usage on `private`).
discard plans;
select set_config('htco.organization_id', 'b8c00000-0000-0000-0000-000000000000', true);
select lives_ok(
  $$ update app.shop_browser_environments set last_health_at = now(), last_health_ok = false
       where id = 'b8c20000-0000-0000-0000-000000000001';
     insert into app.browser_environment_events
       (id, organization_id, shop_id, environment_id, kind, health_ok, problems, actor_kind,
        actor_id, correlation_id, occurred_at)
     values ('b8c30000-0000-0000-0000-000000000001', 'b8c00000-0000-0000-0000-000000000000',
             'b8c10000-0000-0000-0000-000000000001', 'b8c20000-0000-0000-0000-000000000001',
             'health', false, '{egress_ip_mismatch}', 'system', 'browser-worker', 'test:1', now()) $$,
  'AC-8A.3.3: it records a health report in its organization'
);
select lives_ok(
  $$ update app.shop_browser_environments set status = 'degraded'
       where id = 'b8c20000-0000-0000-0000-000000000001' $$,
  'AC-8A.3.3: it moves ready to degraded'
);
select lives_ok(
  $$ update app.shop_browser_environments set status = 'ready'
       where id = 'b8c20000-0000-0000-0000-000000000001' $$,
  'AC-8A.3.3: and degraded back to ready'
);
select lives_ok(
  $$ update app.shop_browser_environments set status = 'login_required'
       where id = 'b8c20000-0000-0000-0000-000000000002' $$,
  'AC-8A.3.2: it moves a provisioning environment to login_required'
);
select throws_ok(
  $$ update app.shop_browser_environments set status = 'ready'
       where id = 'b8c20000-0000-0000-0000-000000000002' $$,
  '42501', null, 'it never signs an environment back in (a person does)'
);
select throws_ok(
  $$ update app.shop_browser_environments set status = 'suspended'
       where id = 'b8c20000-0000-0000-0000-000000000001' $$,
  '42501', null, 'it never suspends an environment'
);
select throws_ok(
  $$ update app.shop_browser_environments set status = 'not_configured'
       where id = 'b8c20000-0000-0000-0000-000000000001' $$,
  '42501', null, 'it never deletes an environment'
);
select throws_ok(
  $$ update app.shop_browser_environments set consent_text_version = 'v9'
       where id = 'b8c20000-0000-0000-0000-000000000001' $$,
  '42501', null, 'it never writes the consent record'
);
select lives_ok(
  $$ insert into app.browser_environment_events
       (id, organization_id, shop_id, environment_id, kind, from_status, to_status, actor_kind,
        actor_id, correlation_id, reason, occurred_at)
     values ('b8c30000-0000-0000-0000-000000000002', 'b8c00000-0000-0000-0000-000000000000',
             'b8c10000-0000-0000-0000-000000000001', 'b8c20000-0000-0000-0000-000000000001',
             'transition', 'ready', 'degraded', 'system', 'browser-worker', 'test:2',
             'health_failed_twice', now()) $$,
  'it appends its transition rows'
);
select throws_ok(
  $$ insert into app.browser_environment_events
       (id, organization_id, shop_id, environment_id, kind, health_ok, actor_kind, actor_id,
        correlation_id, occurred_at)
     values ('b8c30000-0000-0000-0000-000000000003', 'b8c00000-0000-0000-0000-000000000000',
             'b8c10000-0000-0000-0000-000000000001', 'b8c20000-0000-0000-0000-000000000001',
             'health', true, 'user', 'someone', 'test:3', now()) $$,
  '42501', null, 'it never writes a history row in a person''s name'
);
select throws_ok(
  $$ insert into app.browser_environment_events
       (id, organization_id, shop_id, environment_id, kind, from_status, to_status, actor_kind,
        actor_id, correlation_id, reason, occurred_at)
     values ('b8c30000-0000-0000-0000-000000000004', 'b8c00000-0000-0000-0000-000000000000',
             'b8c10000-0000-0000-0000-000000000001', 'b8c20000-0000-0000-0000-000000000001',
             'transition', 'ready', 'suspended', 'system', 'browser-worker', 'test:4',
             'forged', now()) $$,
  '42501', null, 'nor a transition row for an edge it cannot take'
);
select throws_ok(
  $$ insert into app.browser_environment_events
       (id, organization_id, shop_id, environment_id, kind, health_ok, actor_kind, actor_id,
        correlation_id, occurred_at)
     values ('b8c30000-0000-0000-0000-000000000005', 'b8d00000-0000-0000-0000-000000000000',
             'b8d10000-0000-0000-0000-000000000001', 'b8d20000-0000-0000-0000-000000000001',
             'health', true, 'system', 'browser-worker', 'test:5', now()) $$,
  '42501', null, 'nor in another organization'
);
select results_eq(
  $$ select id from app.browser_environment_events order by id $$,
  $$ values ('b8c30000-0000-0000-0000-000000000001'::uuid), ('b8c30000-0000-0000-0000-000000000002'::uuid) $$,
  'it reads its organization''s history only'
);
update app.shop_browser_environments set status = 'degraded'
  where id = 'b8d20000-0000-0000-0000-000000000001';
-- The status event goes only through `app.browser_publish_status_change` (sage on #170).
insert into app.browser_environment_events
  (id, organization_id, shop_id, environment_id, kind, from_status, to_status, actor_kind,
   actor_id, correlation_id, reason, occurred_at)
values ('b8c30000-0000-0000-0000-000000000006', 'b8c00000-0000-0000-0000-000000000000',
        'b8c10000-0000-0000-0000-000000000002', 'b8c20000-0000-0000-0000-000000000002',
        'transition', 'provisioning', 'login_required', 'system', 'browser-worker', 'test:6',
        'signed_out', now());
select lives_ok(
  $$ select app.browser_publish_status_change('b8c30000-0000-0000-0000-000000000006') $$,
  'it publishes the event of a status change it recorded'
);
select is(
  app.browser_publish_status_change('b8c30000-0000-0000-0000-000000000006'),
  app.browser_publish_status_change('b8c30000-0000-0000-0000-000000000006'),
  'publishing the same change again returns the same message'
);
select throws_ok(
  $$ select app.browser_publish_status_change('b8c30000-0000-0000-0000-000000000002') $$,
  '42501', null, 'never the event of a change the environment has since left'
);
select throws_ok(
  $$ select app.browser_publish_status_change('b8c30000-0000-0000-0000-000000000001') $$,
  '42501', null, 'never an event for a health row'
);
select throws_ok(
  $$ select app.browser_publish_status_change('b8d30000-0000-0000-0000-000000000001') $$,
  '42501', null, 'never another organization''s change'
);
select throws_ok(
  $$ insert into jobs.outbox (id, organization_id, shop_id, destination, idempotency_key, payload,
                              next_attempt_at)
     values ('b8c40000-0000-0000-0000-000000000001', 'b8c00000-0000-0000-0000-000000000000',
             'b8c10000-0000-0000-0000-000000000001', 'events.publish', 'test:status:1',
             '{"event_type": "incidents.opened.v1"}'::jsonb, now()) $$,
  '42501', null, 'it writes no outbox message of its own (no forged event)'
);
select throws_ok(
  $$ insert into jobs.jobs (id, kind, idempotency_key, organization_id, payload, max_attempts,
                            initial_backoff_ms, max_backoff_ms, run_at)
     values ('b8c50000-0000-0000-0000-000000000002', 'outbox.deliver', 'test:run:1',
             'b8c00000-0000-0000-0000-000000000000',
             '{"outbox_id": "b8d40000-0000-0000-0000-000000000001"}'::jsonb, 1, 1000, 60000,
             now()) $$,
  '42501', null, 'nor enqueues any job (no crafted delivery naming another message)'
);
reset role;
select is(
  (select status from app.shop_browser_environments
   where id = 'b8d20000-0000-0000-0000-000000000001'),
  'ready', 'it never moves another organization''s environment'
);
select results_eq(
  $$ select o.destination, o.payload ->> 'event_type', o.payload ->> 'to_status',
            o.organization_id, j.kind, j.payload ->> 'outbox_id' = o.id::text
     from jobs.outbox o join jobs.jobs j on j.idempotency_key = 'outbox:' || o.id
     where o.idempotency_key like 'browser.environment_status_changed.v1:%' $$,
  $$ values ('events.publish', 'browser.environment_status_changed.v1', 'login_required',
             'b8c00000-0000-0000-0000-000000000000'::uuid, 'outbox.deliver', true) $$,
  'one status event and one delivery job, built from the change row'
);
-- A delivery job that dies marks only its own organization's message dead (Codex P1 on #170).
insert into jobs.outbox (id, organization_id, shop_id, destination, idempotency_key, payload,
                         next_attempt_at)
values ('b8d40000-0000-0000-0000-000000000001', 'b8d00000-0000-0000-0000-000000000000',
        'b8d10000-0000-0000-0000-000000000001', 'events.publish', 'test:other:1',
        '{}'::jsonb, now());
insert into jobs.jobs (id, kind, idempotency_key, organization_id, payload, max_attempts,
                       initial_backoff_ms, max_backoff_ms, run_at)
values ('b8c50000-0000-0000-0000-000000000003', 'outbox.deliver', 'test:cross:1',
        'b8c00000-0000-0000-0000-000000000000',
        '{"outbox_id": "b8d40000-0000-0000-0000-000000000001"}'::jsonb, 1, 1000, 60000, now());
update jobs.jobs set state = 'dead' where id = 'b8c50000-0000-0000-0000-000000000003';
select is(
  (select status from jobs.outbox where id = 'b8d40000-0000-0000-0000-000000000001'),
  'pending', 'another organization''s job never marks a message dead'
);


select * from finish();
rollback;
