-- Row-level security as a signed-in user (TESTING_STRATEGY §8, architecture audit P1-7) for six
-- tables whose pgTAP files ran only as the worker or checked only that RLS was on: order items,
-- the order sync state, automation executions, action executions, order status events and
-- financial entries. Organization K has two shops; its Owner sees both, its Support member is
-- granted one shop and sees that shop's rows only, and organization L's Owner sees none of K's.
begin;
create extension if not exists pgtap with schema extensions;
select plan(24);

insert into auth.users (id, email) values
  ('21e00000-0000-0000-0000-000000000001', 'owner-k@example.test'),
  ('21e00000-0000-0000-0000-000000000002', 'support-k@example.test'),
  ('21f00000-0000-0000-0000-000000000001', 'owner-l@example.test');
insert into app.organizations (id, name) values
  ('21a00000-0000-0000-0000-000000000000', 'Organization K'),
  ('21b00000-0000-0000-0000-000000000000', 'Organization L');
insert into app.memberships (id, organization_id, user_id, role) values
  ('21d00000-0000-0000-0000-000000000001', '21a00000-0000-0000-0000-000000000000', '21e00000-0000-0000-0000-000000000001', 'owner'),
  ('21d00000-0000-0000-0000-000000000002', '21a00000-0000-0000-0000-000000000000', '21e00000-0000-0000-0000-000000000002', 'support'),
  ('21d00000-0000-0000-0000-000000000003', '21b00000-0000-0000-0000-000000000000', '21f00000-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('21c00000-0000-0000-0000-000000000001', '21a00000-0000-0000-0000-000000000000', 'Shop K1', 'etsy', 'active'),
  ('21c00000-0000-0000-0000-000000000002', '21a00000-0000-0000-0000-000000000000', 'Shop K2', 'etsy', 'active'),
  ('21c00000-0000-0000-0000-000000000003', '21b00000-0000-0000-0000-000000000000', 'Shop L1', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('21d10000-0000-0000-0000-000000000002', '21a00000-0000-0000-0000-000000000000',
   '21d00000-0000-0000-0000-000000000002', '21c00000-0000-0000-0000-000000000001');

-- One order per shop, with one item, a sync state, a status event and a revenue line.
insert into app.etsy_orders
  (organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
   grand_total_amount, grand_total_divisor, grand_total_currency,
   subtotal_amount, subtotal_divisor, subtotal_currency,
   shipping_amount, shipping_divisor, shipping_currency,
   discount_amount, discount_divisor, discount_currency,
   etsy_created_at, etsy_updated_at, synced_at, order_state)
select o, s, e, r, 'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100,
       'USD', now(), now(), now(), 'awaiting_shipment'
from (values
  ('21a00000-0000-0000-0000-000000000000'::uuid, '21c00000-0000-0000-0000-000000000001'::uuid, 2101, 21001),
  ('21a00000-0000-0000-0000-000000000000'::uuid, '21c00000-0000-0000-0000-000000000002'::uuid, 2102, 21002),
  ('21b00000-0000-0000-0000-000000000000'::uuid, '21c00000-0000-0000-0000-000000000003'::uuid, 2103, 21003)
) as t(o, s, e, r);
insert into app.etsy_order_items
  (organization_id, shop_id, receipt_id, transaction_id, position, title, quantity,
   unit_price_amount, unit_price_divisor, unit_price_currency, is_digital)
select organization_id, shop_id, receipt_id, receipt_id * 10, 0, 'Wallet', 1, 100, 100, 'USD', false
from app.etsy_orders where receipt_id in (21001, 21002, 21003);
insert into app.etsy_order_sync_state (shop_id, organization_id, etsy_shop_id)
select shop_id, organization_id, etsy_shop_id
from app.etsy_orders where receipt_id in (21001, 21002, 21003);
insert into app.order_status_events
  (id, organization_id, shop_id, receipt_id, from_state, to_state, source, observed_at,
   etsy_updated_at, receipt_sha256)
select gen_random_uuid(), organization_id, shop_id, receipt_id, null, 'paid', 'webhook', now(),
       now(), decode(repeat('ab', 32), 'hex')
from app.etsy_orders where receipt_id in (21001, 21002, 21003);
insert into app.financial_entries
  (id, organization_id, shop_id, receipt_id, kind, position, amount, currency, source, occurred_at)
select gen_random_uuid(), organization_id, shop_id, receipt_id, 'revenue', 0, 1, 'USD',
       'etsy_receipt', now()
from app.etsy_orders where receipt_id in (21001, 21002, 21003);

-- One automation execution and one action execution per shop.
insert into app.automation_definitions
  (id, version, name, business_owner, primary_trigger, callable_from, retry_max_attempts,
   retry_initial_backoff_seconds, retry_max_backoff_seconds, timeout_seconds, side_effect,
   approval_required, readiness, activation_phase, specification, document, specification_hash,
   registry_commit, synced_at)
values ('pgtap-rls', 1, 'pgTAP RLS', 'owner', 'event', '{event}', 2, 1, 10, 30, 'internal',
        false, 'draft', 5, '{}', '{}', repeat('a', 64), 'local', now());
insert into app.automation_executions
  (id, organization_id, shop_id, automation_id, automation_version, idempotency_key, trigger,
   actor_kind, actor_id, state, correlation_id)
select gen_random_uuid(), organization_id, shop_id, 'pgtap-rls', 1, 'k-' || receipt_id, 'event',
       'system', 'test', 'pending', 'c-' || receipt_id
from app.etsy_orders where receipt_id in (21001, 21002, 21003);
insert into app.action_executions
  (id, organization_id, shop_id, action_name, action_version, idempotency_key, request_hash,
   state, outcome, actor_kind, actor_id, correlation_id)
select gen_random_uuid(), organization_id, shop_id, 'etsy.receipts.read', 1, 'k-' || receipt_id,
       repeat('a', 64), 'succeeded', 'succeeded', 'automation', 'etsy-order-intake',
       'c-' || receipt_id
from app.etsy_orders where receipt_id in (21001, 21002, 21003);

-- Owner K: both of organization K's shops, nothing of L.
select set_config('request.jwt.claims', '{"sub":"21e00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select receipt_id from app.etsy_order_items order by 1 $$,
  $$ values (21001::bigint), (21002::bigint) $$, 'Owner K: order items of both K shops');
select results_eq($$ select etsy_shop_id from app.etsy_order_sync_state order by 1 $$,
  $$ values (2101::bigint), (2102::bigint) $$, 'Owner K: the sync state of both K shops');
select results_eq($$ select receipt_id from app.order_status_events order by 1 $$,
  $$ values (21001::bigint), (21002::bigint) $$, 'Owner K: status events of both K shops');
select results_eq($$ select receipt_id from app.financial_entries order by 1 $$,
  $$ values (21001::bigint), (21002::bigint) $$, 'Owner K: financial entries of both K shops');
select results_eq($$ select idempotency_key from app.automation_executions order by 1 $$,
  $$ values ('k-21001'), ('k-21002') $$, 'Owner K: automation executions of organization K');
select results_eq($$ select idempotency_key from app.action_executions order by 1 $$,
  $$ values ('k-21001'), ('k-21002') $$, 'Owner K: action executions of organization K');
reset role;

-- Support K: granted Shop K1 only.
select set_config('request.jwt.claims', '{"sub":"21e00000-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select receipt_id from app.etsy_order_items $$,
  $$ values (21001::bigint) $$, 'Support K: order items of the granted shop only');
select results_eq($$ select etsy_shop_id from app.etsy_order_sync_state $$,
  $$ values (2101::bigint) $$, 'Support K: the granted shop''s sync state only');
select results_eq($$ select receipt_id from app.order_status_events $$,
  $$ values (21001::bigint) $$, 'Support K: status events of the granted shop only');
select results_eq($$ select receipt_id from app.financial_entries $$,
  $$ values (21001::bigint) $$, 'Support K: financial entries of the granted shop only');
select results_eq($$ select idempotency_key from app.automation_executions $$,
  $$ values ('k-21001') $$, 'Support K: automation executions of the granted shop only');
select results_eq($$ select idempotency_key from app.action_executions $$,
  $$ values ('k-21001') $$, 'Support K: action executions of the granted shop only');
reset role;

-- Owner L: organization L only.
select set_config('request.jwt.claims', '{"sub":"21f00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select receipt_id from app.etsy_order_items $$,
  $$ values (21003::bigint) $$, 'Owner L: no order item of organization K');
select results_eq($$ select etsy_shop_id from app.etsy_order_sync_state $$,
  $$ values (2103::bigint) $$, 'Owner L: no sync state of organization K');
select results_eq($$ select receipt_id from app.order_status_events $$,
  $$ values (21003::bigint) $$, 'Owner L: no status event of organization K');
select results_eq($$ select receipt_id from app.financial_entries $$,
  $$ values (21003::bigint) $$, 'Owner L: no financial entry of organization K');
select results_eq($$ select idempotency_key from app.automation_executions $$,
  $$ values ('k-21003') $$, 'Owner L: no automation execution of organization K');
select results_eq($$ select idempotency_key from app.action_executions $$,
  $$ values ('k-21003') $$, 'Owner L: no action execution of organization K');

-- Signed-in users only read these tables.
select throws_ok($$ delete from app.order_status_events $$, '42501', null,
  'a user cannot delete status events');
select throws_ok($$ update app.financial_entries set amount = 2 $$, '42501', null,
  'a user cannot change financial entries');
select throws_ok($$ update app.action_executions set state = 'failed' $$, '42501', null,
  'a user cannot change action executions');
select throws_ok($$ update app.automation_executions set state = 'cancelled' $$, '42501', null,
  'a user cannot change automation executions');
select throws_ok($$ delete from app.etsy_order_items $$, '42501', null,
  'a user cannot delete order items');
select throws_ok($$ update app.etsy_order_sync_state set backfill_offset = 1 $$, '42501', null,
  'a user cannot change the sync state');
reset role;

select * from finish();
rollback;
