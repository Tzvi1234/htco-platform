-- Browser sessions (slice 8A.2): every Browser Controller session recorded with a purpose code
-- and an outcome, final once ended, bound to its environment; the error counter; the browser
-- role writes its organization's sessions only, and the Owner and Admins read theirs.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_browser;
select plan(17);

insert into auth.users (id, email) values
  ('b9a00000-0000-0000-0000-0000000000a1', 'owner-bs@example.test'),
  ('b9a00000-0000-0000-0000-0000000000a2', 'support-bs@example.test');
insert into app.organizations (id, name) values
  ('b9a00000-0000-0000-0000-000000000000', 'Organization Sessions A'),
  ('b9b00000-0000-0000-0000-000000000000', 'Organization Sessions B');
insert into app.memberships (id, organization_id, user_id, role) values
  ('b9a00000-0000-0000-0000-0000000000b1', 'b9a00000-0000-0000-0000-000000000000', 'b9a00000-0000-0000-0000-0000000000a1', 'owner'),
  ('b9a00000-0000-0000-0000-0000000000b2', 'b9a00000-0000-0000-0000-000000000000', 'b9a00000-0000-0000-0000-0000000000a2', 'support');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('b9a10000-0000-0000-0000-000000000001', 'b9a00000-0000-0000-0000-000000000000', 'Shop SA', 'etsy', 'active'),
  ('b9b10000-0000-0000-0000-000000000001', 'b9b00000-0000-0000-0000-000000000000', 'Shop SB', 'etsy', 'active');
insert into app.shop_browser_environments (id, organization_id, shop_id, status) values
  ('b9a20000-0000-0000-0000-000000000001', 'b9a00000-0000-0000-0000-000000000000',
   'b9a10000-0000-0000-0000-000000000001', 'provisioning'),
  ('b9b20000-0000-0000-0000-000000000001', 'b9b00000-0000-0000-0000-000000000000',
   'b9b10000-0000-0000-0000-000000000001', 'provisioning');
insert into app.browser_sessions
  (id, organization_id, shop_id, environment_id, purpose, outcome, failure, started_at, ended_at)
values
  ('b9a30000-0000-0000-0000-000000000001', 'b9a00000-0000-0000-0000-000000000000',
   'b9a10000-0000-0000-0000-000000000001', 'b9a20000-0000-0000-0000-000000000001',
   'health.check', 'failed', 'timeout', now() - interval '1 minute', now()),
  ('b9b30000-0000-0000-0000-000000000001', 'b9b00000-0000-0000-0000-000000000000',
   'b9b10000-0000-0000-0000-000000000001', 'b9b20000-0000-0000-0000-000000000001',
   'health.check', 'running', null, now(), null);

-- Structure and grants.
select ok((select relrowsecurity from pg_class where oid = 'app.browser_sessions'::regclass),
  'browser_sessions has RLS enabled');
select ok(
  not has_table_privilege('anon', 'app.browser_sessions', 'select, insert, update, delete')
  and not has_table_privilege('authenticated', 'app.browser_sessions', 'insert, update, delete'),
  'no client role writes sessions'
);
select ok(not has_table_privilege('htco_browser', 'app.browser_sessions', 'delete'),
  'the browser role never deletes a session');
select ok(not has_table_privilege('htco_worker', 'app.browser_sessions', 'select'),
  'the agent-worker does not see browser sessions');
select col_default_is('app', 'shop_browser_environments', 'error_count', '0',
  'the error counter starts at zero');
select ok(has_column_privilege('htco_browser', 'app.shop_browser_environments', 'error_count', 'update')
  and not has_column_privilege('htco_browser', 'app.shop_browser_environments', 'status', 'update'),
  'the browser role counts errors but still cannot change a status');

-- Shape: a purpose code, a failure code only on failure, final once ended.
select throws_ok(
  $$ insert into app.browser_sessions (id, organization_id, shop_id, environment_id, purpose, started_at)
     values (gen_random_uuid(), 'b9a00000-0000-0000-0000-000000000000', 'b9a10000-0000-0000-0000-000000000001',
             'b9a20000-0000-0000-0000-000000000001', 'Open the page https://example.test', now()) $$,
  '23514', null, 'a purpose is a dotted code, never free text'
);
select throws_ok(
  $$ insert into app.browser_sessions (id, organization_id, shop_id, environment_id, purpose, outcome, failure, started_at, ended_at)
     values (gen_random_uuid(), 'b9a00000-0000-0000-0000-000000000000', 'b9a10000-0000-0000-0000-000000000001',
             'b9a20000-0000-0000-0000-000000000001', 'test.run', 'failed', 'Page said: password wrong', now(), now()) $$,
  '23514', null, 'a failure is a code, never page text'
);
select throws_ok(
  $$ insert into app.browser_sessions (id, organization_id, shop_id, environment_id, purpose, outcome, started_at, ended_at)
     values (gen_random_uuid(), 'b9a00000-0000-0000-0000-000000000000', 'b9a10000-0000-0000-0000-000000000001',
             'b9a20000-0000-0000-0000-000000000001', 'test.run', 'failed', now(), now()) $$,
  '23514', null, 'a failed session names its failure'
);
select throws_ok(
  $$ insert into app.browser_sessions (id, organization_id, shop_id, environment_id, purpose, started_at)
     values (gen_random_uuid(), 'b9a00000-0000-0000-0000-000000000000', 'b9b10000-0000-0000-0000-000000000001',
             'b9a20000-0000-0000-0000-000000000001', 'test.run', now()) $$,
  '23503', null, 'a session cannot name another shop than its environment''s'
);
select throws_ok(
  $$ update app.browser_sessions set outcome = 'succeeded', failure = null
     where id = 'b9a30000-0000-0000-0000-000000000001' $$,
  '23514', null, 'an ended session is final'
);
select throws_ok(
  $$ update app.browser_sessions set purpose = 'other.purpose'
     where id = 'b9b30000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a running session only ends'
);

-- The browser role: its organization only.
set local role htco_browser;
select set_config('htco.organization_id', 'b9a00000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select id from app.browser_sessions $$,
  $$ values ('b9a30000-0000-0000-0000-000000000001'::uuid) $$,
  'the browser role sees its organization''s sessions only'
);
select lives_ok(
  $$ insert into app.browser_sessions (id, organization_id, shop_id, environment_id, purpose, started_at)
     values ('b9a30000-0000-0000-0000-000000000002', 'b9a00000-0000-0000-0000-000000000000',
             'b9a10000-0000-0000-0000-000000000001', 'b9a20000-0000-0000-0000-000000000001', 'test.run', now());
     update app.browser_sessions set outcome = 'failed', failure = 'timeout', ended_at = now()
       where id = 'b9a30000-0000-0000-0000-000000000002';
     update app.shop_browser_environments set error_count = error_count + 1, last_session_at = now()
       where id = 'b9a20000-0000-0000-0000-000000000001' $$,
  'AC-8A.2.3: the browser role records a timed-out session and counts the error'
);
update app.browser_sessions set outcome = 'failed', failure = 'stolen', ended_at = now()
  where id = 'b9b30000-0000-0000-0000-000000000001';
reset role;
select is(
  (select outcome from app.browser_sessions where id = 'b9b30000-0000-0000-0000-000000000001'),
  'running', 'it cannot end another organization''s session'
);
select is(
  (select error_count from app.shop_browser_environments where id = 'b9a20000-0000-0000-0000-000000000001'),
  1, 'the error counter incremented'
);

-- The Owner reads the organization's sessions; Support does not.
set local role authenticated;
select set_config('request.jwt.claims',
  '{"sub": "b9a00000-0000-0000-0000-0000000000a1", "role": "authenticated"}', true);
select is((select count(*)::int from app.browser_sessions), 2,
  'the Owner reads the organization''s sessions only');
reset role;

select * from finish();
rollback;
