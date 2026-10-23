-- The Shop Environment Registry (slice 5.16): the transition table enforced by the database
-- (AC-5.16.1), an append-only history (AC-5.16.2), and tenant isolation: organization A cannot
-- read or change B's environment (AC-5.16.3).
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(24);

insert into auth.users (id, email) values
  ('e1111111-0000-0000-0000-000000000001', 'owner-e@example.test'),
  ('e1111111-0000-0000-0000-000000000002', 'support-e@example.test'),
  ('e2222222-0000-0000-0000-000000000001', 'owner-f@example.test');
insert into app.organizations (id, name) values
  ('ea000000-0000-0000-0000-000000000000', 'Organization E'),
  ('eb000000-0000-0000-0000-000000000000', 'Organization F');
insert into app.memberships (id, organization_id, user_id, role) values
  ('ee000000-0000-0000-0000-000000000001', 'ea000000-0000-0000-0000-000000000000', 'e1111111-0000-0000-0000-000000000001', 'owner'),
  ('ee000000-0000-0000-0000-000000000002', 'ea000000-0000-0000-0000-000000000000', 'e1111111-0000-0000-0000-000000000002', 'support'),
  ('ef000000-0000-0000-0000-000000000001', 'eb000000-0000-0000-0000-000000000000', 'e2222222-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('ec000000-0000-0000-0000-000000000001', 'ea000000-0000-0000-0000-000000000000', 'Shop E', 'etsy', 'active'),
  ('ec000000-0000-0000-0000-000000000002', 'ea000000-0000-0000-0000-000000000000', 'Shop E2', 'etsy', 'active'),
  ('ed000000-0000-0000-0000-000000000001', 'eb000000-0000-0000-0000-000000000000', 'Shop F', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('ee100000-0000-0000-0000-000000000002', 'ea000000-0000-0000-0000-000000000000', 'ee000000-0000-0000-0000-000000000002', 'ec000000-0000-0000-0000-000000000001');
insert into app.shop_browser_environments (id, organization_id, shop_id, status) values
  ('ec100000-0000-0000-0000-000000000001', 'ea000000-0000-0000-0000-000000000000',
   'ec000000-0000-0000-0000-000000000001', 'provisioning'),
  ('ed100000-0000-0000-0000-000000000001', 'eb000000-0000-0000-0000-000000000000',
   'ed000000-0000-0000-0000-000000000001', 'provisioning');
insert into app.browser_environment_events
  (id, organization_id, shop_id, environment_id, kind, from_status, to_status, actor_kind,
   actor_id, correlation_id, reason, occurred_at)
values
  ('ec200000-0000-0000-0000-000000000001', 'ea000000-0000-0000-0000-000000000000',
   'ec000000-0000-0000-0000-000000000001', 'ec100000-0000-0000-0000-000000000001', 'transition',
   'not_configured', 'provisioning', 'user', 'e1111111-0000-0000-0000-000000000001', 'c-e',
   'created', now()),
  ('ed200000-0000-0000-0000-000000000001', 'eb000000-0000-0000-0000-000000000000',
   'ed000000-0000-0000-0000-000000000001', 'ed100000-0000-0000-0000-000000000001', 'transition',
   'not_configured', 'provisioning', 'user', 'e2222222-0000-0000-0000-000000000001', 'c-f',
   'created', now());

-- Structure and grants.
select ok((select relrowsecurity from pg_class where oid = 'app.shop_browser_environments'::regclass),
  'shop_browser_environments has RLS enabled');
select ok((select relrowsecurity from pg_class where oid = 'app.browser_environment_events'::regclass),
  'browser_environment_events has RLS enabled');
select ok(not has_table_privilege('authenticated', 'app.shop_browser_environments', 'insert, update, delete'),
  'users cannot write environments');
select ok(not has_table_privilege('authenticated', 'app.browser_environment_events', 'insert, update, delete'),
  'users cannot write the history');
select ok(not has_column_privilege('authenticated', 'app.shop_browser_environments', 'consent_ip_hash', 'select'),
  'users never read the consent record directly');
select ok(not has_table_privilege('anon', 'app.shop_browser_environments', 'select'),
  'anon reads nothing');
select hasnt_column('app', 'shop_browser_environments', 'proxy', 'no proxy column (ADR-0020)');
select hasnt_column('app', 'shop_browser_environments', 'fingerprint', 'no fingerprint column (ADR-0020)');

-- AC-5.16.1: exactly the table's 20 edges are allowed, every other pair is refused.
select results_eq(
  $$ select count(*)::int from
       unnest(array['not_configured', 'provisioning', 'ready', 'login_required', 'degraded',
                    'suspended', 'failed']) a,
       unnest(array['not_configured', 'provisioning', 'ready', 'login_required', 'degraded',
                    'suspended', 'failed']) b
     where private.browser_environment_edge_allowed(a, b) $$,
  $$ values (20) $$,
  'AC-5.16.1: the database allows the 20 edges of the transition table'
);
select ok(not private.browser_environment_edge_allowed('suspended', 'degraded'),
  'suspended -> degraded is not an edge');
select throws_ok(
  $$ insert into app.shop_browser_environments (id, organization_id, shop_id, status)
     values ('ec100000-0000-0000-0000-000000000002', 'ea000000-0000-0000-0000-000000000000',
             'ec000000-0000-0000-0000-000000000002', 'ready') $$,
  '23514', null, 'AC-5.16.1: an environment is created provisioning, never ready'
);
select lives_ok(
  $$ update app.shop_browser_environments set status = 'ready'
     where id = 'ec100000-0000-0000-0000-000000000001' $$,
  'AC-5.16.1: provisioning -> ready is allowed'
);
select throws_ok(
  $$ update app.shop_browser_environments set status = 'provisioning'
     where id = 'ec100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'AC-5.16.1: ready -> provisioning is refused'
);
select throws_ok(
  $$ insert into app.shop_browser_environments (id, organization_id, shop_id, status)
     values ('ec100000-0000-0000-0000-000000000003', 'ea000000-0000-0000-0000-000000000000',
             'ec000000-0000-0000-0000-000000000001', 'provisioning') $$,
  '23505', null, 'one environment per shop'
);
select throws_ok(
  $$ insert into app.shop_browser_environments (id, organization_id, shop_id, status,
       consent_text_version)
     values ('ec100000-0000-0000-0000-000000000004', 'ea000000-0000-0000-0000-000000000000',
             'ec000000-0000-0000-0000-000000000002', 'provisioning', 'v1') $$,
  '23514', null, 'a consent is recorded whole or not at all'
);

-- AC-5.16.2: the history is append-only, and holds the table's edges only.
select throws_ok(
  $$ update app.browser_environment_events set reason = 'changed'
     where id = 'ec200000-0000-0000-0000-000000000001' $$,
  '42501', null, 'AC-5.16.2: a recorded change never changes'
);
select throws_ok(
  $$ delete from app.browser_environment_events where id = 'ec200000-0000-0000-0000-000000000001' $$,
  '42501', null, 'AC-5.16.2: a recorded change is never deleted'
);
select throws_ok(
  $$ insert into app.browser_environment_events
       (id, organization_id, shop_id, environment_id, kind, from_status, to_status, actor_kind,
        actor_id, correlation_id, reason, occurred_at)
     values ('ec200000-0000-0000-0000-000000000002', 'ea000000-0000-0000-0000-000000000000',
             'ec000000-0000-0000-0000-000000000001', 'ec100000-0000-0000-0000-000000000001',
             'transition', 'suspended', 'degraded', 'system', 'x', 'c', 'r', now()) $$,
  '23514', null, 'AC-5.16.2: the history refuses an edge outside the table'
);
select throws_ok(
  $$ insert into app.browser_environment_events
       (id, organization_id, shop_id, environment_id, kind, health_ok, problems, actor_kind,
        actor_id, correlation_id, occurred_at)
     values ('ec200000-0000-0000-0000-000000000003', 'ea000000-0000-0000-0000-000000000000',
             'ec000000-0000-0000-0000-000000000001', 'ec100000-0000-0000-0000-000000000001',
             'health', false, '{"The IP was 203.0.113.9"}', 'system', 'x', 'c', now()) $$,
  '23514', null, 'a health problem is a code, never free text'
);

-- AC-5.16.3: Owner E sees organization E's environment and history only, and changes nothing.
select set_config('request.jwt.claims', '{"sub":"e1111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select results_eq(
  $$ select id, status from app.shop_browser_environments $$,
  $$ values ('ec100000-0000-0000-0000-000000000001'::uuid, 'ready'::text) $$,
  'AC-5.16.3: an Owner sees their own organization''s environments only'
);
select results_eq(
  $$ select id from app.browser_environment_events $$,
  $$ values ('ec200000-0000-0000-0000-000000000001'::uuid) $$,
  'AC-5.16.3: and their own organization''s history only'
);
select throws_ok(
  $$ update app.shop_browser_environments set status = 'suspended'
     where id = 'ed100000-0000-0000-0000-000000000001' $$,
  '42501', null, 'AC-5.16.3: an Owner cannot change another organization''s environment'
);
reset role;

-- Support E has the shop, but the environment is for the Owner and Admins.
select set_config('request.jwt.claims', '{"sub":"e1111111-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is_empty(
  $$ select id from app.shop_browser_environments $$,
  'a Support member sees no environment'
);
reset role;

-- The worker: its job's organization's statuses only.
set local role htco_worker;
select set_config('htco.organization_id', 'eb000000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select id, status from app.shop_browser_environments $$,
  $$ values ('ed100000-0000-0000-0000-000000000001'::uuid, 'provisioning'::text) $$,
  'the worker sees its job''s organization''s environments only'
);
reset role;

select * from finish();
rollback;
