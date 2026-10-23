-- Inbox, outbox and Idempotency-Key storage (slice 5.2): no client role sees them; the inbox is
-- unique per source event; the worker reaches outbox rows of its job's organization only.
begin;
create extension if not exists pgtap with schema extensions;
-- pgTAP lives in `extensions`; the worker role needs to see it while the test runs as that role
-- (rolled back with the test).
grant usage on schema extensions to htco_worker;
select plan(11);

insert into app.organizations (id, name) values
  ('9a000000-0000-0000-0000-000000000000', 'Organization G'),
  ('9b000000-0000-0000-0000-000000000000', 'Organization H');
insert into jobs.outbox (id, organization_id, destination, idempotency_key, payload, next_attempt_at)
values
  ('9a100000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', 'test.sink', 'g-1', '{}', now()),
  ('9b100000-0000-0000-0000-000000000001', '9b000000-0000-0000-0000-000000000000', 'test.sink', 'h-1', '{}', now());
insert into jobs.inbox (id, source, event_id, event_type, payload, payload_hash, received_at)
values ('9c100000-0000-0000-0000-000000000001', 'etsy', 'evt-pgtap-1', 'receipt.updated', '{}',
        decode(repeat('00', 32), 'hex'), now());

select ok(
  (select bool_and(relrowsecurity) from pg_class
   where oid in ('jobs.inbox'::regclass, 'jobs.outbox'::regclass,
                 'private.idempotency_keys'::regclass)),
  'inbox, outbox and idempotency keys have RLS enabled'
);
select ok(not has_table_privilege('authenticated', 'jobs.outbox', 'select'),
  'authenticated cannot read the outbox');
select ok(not has_table_privilege('authenticated', 'private.idempotency_keys', 'select'),
  'authenticated cannot read stored responses');
select ok(not has_table_privilege('htco_worker', 'private.idempotency_keys', 'select'),
  'the worker cannot read stored responses');
select throws_ok(
  $$ insert into jobs.inbox (id, source, event_id, event_type, payload, payload_hash, received_at)
     values ('9c100000-0000-0000-0000-000000000002', 'etsy', 'evt-pgtap-1', 'receipt.updated',
             '{}', decode(repeat('00', 32), 'hex'), now()) $$,
  '23505', null, 'AC-5.2.1: one inbox row per source event'
);
select throws_ok(
  $$ insert into jobs.outbox (id, organization_id, destination, idempotency_key, payload,
                              next_attempt_at)
     values ('9a100000-0000-0000-0000-000000000002', '9a000000-0000-0000-0000-000000000000',
             'test.sink', 'g-1', '{}', now()) $$,
  '23505', null, 'one outbox message per idempotency key'
);
select throws_ok(
  $$ update jobs.outbox set status = 'delivered'
     where id = '9a100000-0000-0000-0000-000000000001' $$,
  '23514', null, 'a delivered message has its delivery time'
);

set local role htco_worker;
select is_empty($$ select id from jobs.outbox $$, 'outside a job the worker sees no outbox row');
select set_config('htco.organization_id', '9a000000-0000-0000-0000-000000000000', true);
select results_eq(
  $$ select id from jobs.outbox $$,
  $$ values ('9a100000-0000-0000-0000-000000000001'::uuid) $$,
  'inside a job the worker sees its organization''s outbox rows only'
);
select is(
  (select count(*)::int from jobs.inbox where event_id = 'evt-pgtap-1'), 1,
  'the worker reads the inbox'
);
select throws_ok(
  $$ insert into jobs.outbox (id, organization_id, destination, idempotency_key, payload,
                              next_attempt_at)
     values ('9b100000-0000-0000-0000-000000000002', '9b000000-0000-0000-0000-000000000000',
             'test.sink', 'h-2', '{}', now()) $$,
  '42501', null, 'the worker cannot write another organization''s outbox'
);
reset role;

select * from finish();
rollback;
