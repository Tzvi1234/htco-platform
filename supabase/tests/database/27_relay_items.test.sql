-- The AutoVA relay (slice 6.10): one relay item per order, a number used for one order of a shop
-- only, members see their shops' items, the full code only the shop's buyer-data roles, and the
-- worker acts in its job's organization and never reads a code.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(15);

insert into auth.users (id, email) values
  ('27a10000-0000-0000-0000-000000000001', 'owner-r@example.test'),
  ('27a10000-0000-0000-0000-000000000002', 'viewer-r@example.test'),
  ('27a20000-0000-0000-0000-000000000001', 'owner-s@example.test');
insert into app.organizations (id, name) values
  ('27b10000-0000-0000-0000-000000000000', 'Organization R'),
  ('27b20000-0000-0000-0000-000000000000', 'Organization S');
insert into app.memberships (id, organization_id, user_id, role) values
  ('27e10000-0000-0000-0000-000000000001', '27b10000-0000-0000-0000-000000000000', '27a10000-0000-0000-0000-000000000001', 'owner'),
  ('27e10000-0000-0000-0000-000000000002', '27b10000-0000-0000-0000-000000000000', '27a10000-0000-0000-0000-000000000002', 'viewer'),
  ('27e20000-0000-0000-0000-000000000001', '27b20000-0000-0000-0000-000000000000', '27a20000-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('27c10000-0000-0000-0000-000000000001', '27b10000-0000-0000-0000-000000000000', 'Shop R', 'etsy', 'active'),
  ('27c20000-0000-0000-0000-000000000001', '27b20000-0000-0000-0000-000000000000', 'Shop S', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('27f10000-0000-0000-0000-000000000001', '27b10000-0000-0000-0000-000000000000',
   '27e10000-0000-0000-0000-000000000002', '27c10000-0000-0000-0000-000000000001');
insert into app.relay_items (id, organization_id, shop_id, receipt_id, state, carrier,
                             tracking_fingerprint, tracking_last4, paid_at, opened_at, updated_at) values
  ('27d10000-0000-0000-0000-000000000001', '27b10000-0000-0000-0000-000000000000',
   '27c10000-0000-0000-0000-000000000001', 501, 'handed_off', 'usps',
   decode(repeat('ab', 32), 'hex'), '1234', now(), now(), now()),
  ('27d20000-0000-0000-0000-000000000001', '27b20000-0000-0000-0000-000000000000',
   '27c20000-0000-0000-0000-000000000001', 601, 'awaiting_tracking', null, null, null, now(), now(), now());
insert into app.relay_item_codes (relay_item_id, organization_id, shop_id, code) values
  ('27d10000-0000-0000-0000-000000000001', '27b10000-0000-0000-0000-000000000000',
   '27c10000-0000-0000-0000-000000000001', '9400100000000000001234');

-- Constraints --------------------------------------------------------------------------------
select throws_ok(
  $$insert into app.relay_items (id, organization_id, shop_id, receipt_id, state, paid_at, opened_at, updated_at)
    values (gen_random_uuid(), '27b10000-0000-0000-0000-000000000000',
            '27c10000-0000-0000-0000-000000000001', 501, 'awaiting_tracking', now(), now(), now())$$,
  '23505', null, 'one relay item per order (AC-6.10.1)'
);
select throws_ok(
  $$insert into app.relay_items (id, organization_id, shop_id, receipt_id, state, carrier,
                                 tracking_fingerprint, tracking_last4, paid_at, opened_at, updated_at)
    values (gen_random_uuid(), '27b10000-0000-0000-0000-000000000000',
            '27c10000-0000-0000-0000-000000000001', 502, 'handed_off', 'usps',
            decode(repeat('ab', 32), 'hex'), '1234', now(), now(), now())$$,
  '23505', null, 'a number is pasted for one order of a shop only (AC-6.10.2)'
);
select throws_ok(
  $$insert into app.relay_items (id, organization_id, shop_id, receipt_id, state, paid_at, opened_at, updated_at)
    values (gen_random_uuid(), '27b10000-0000-0000-0000-000000000000',
            '27c10000-0000-0000-0000-000000000001', 503, 'handed_off', now(), now(), now())$$,
  '23514', null, 'handed off only with the pasted tracking'
);
select throws_ok(
  $$insert into app.relay_items (id, organization_id, shop_id, receipt_id, state, paid_at, opened_at, updated_at)
    values (gen_random_uuid(), '27b10000-0000-0000-0000-000000000000',
            '27c10000-0000-0000-0000-000000000001', 504, 'closed', now(), now(), now())$$,
  '23514', null, 'a closed item says why and when'
);
select throws_ok(
  $$insert into app.relay_items (id, organization_id, shop_id, receipt_id, state, paid_at, opened_at, updated_at)
    values (gen_random_uuid(), '27b10000-0000-0000-0000-000000000000',
            '27c20000-0000-0000-0000-000000000001', 505, 'awaiting_tracking', now(), now(), now())$$,
  '23503', null, 'the shop belongs to the item''s organization'
);
select throws_ok(
  $$insert into app.relay_item_codes (relay_item_id, organization_id, shop_id, code)
    values ('27d20000-0000-0000-0000-000000000001', '27b20000-0000-0000-0000-000000000000',
            '27c20000-0000-0000-0000-000000000001', 'not a code; drop table')$$,
  '23514', null, 'a code is a normalised tracking code only'
);

-- Members ------------------------------------------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"27a10000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*)::int from app.relay_items), 1, 'the owner sees their shop''s item only');
select is((select code from app.relay_item_codes), '9400100000000000001234',
  'the owner (a buyer-data role) reads the code');
reset role;

select set_config('request.jwt.claims', '{"sub":"27a10000-0000-0000-0000-000000000002","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::int from app.relay_items), 1, 'a viewer with the shop sees its item');
select is_empty($$ select code from app.relay_item_codes $$,
  'a viewer (not a buyer-data role) never reads the code');
reset role;

-- The worker ---------------------------------------------------------------------------------
select set_config('htco.organization_id', '27b10000-0000-0000-0000-000000000000', true);
set local role htco_worker;
select is((select count(*)::int from app.relay_items), 1, 'the worker sees its job''s organization only');
select throws_ok($$ select code from app.relay_item_codes $$, '42501', null,
  'the worker never reads a code');
select lives_ok(
  $$update app.relay_items set state = 'closed', closed_reason = 'tracking_on_etsy', closed_at = now(), updated_at = now()
    where id = '27d10000-0000-0000-0000-000000000001'$$,
  'the worker closes an item on evidence'
);
select throws_ok(
  $$update app.relay_items set carrier = 'ups' where id = '27d10000-0000-0000-0000-000000000001'$$,
  '42501', null, 'the worker never changes the pasted tracking'
);
select is(
  (with gone as (
     delete from app.relay_item_codes where relay_item_id = '27d10000-0000-0000-0000-000000000001'
     returning relay_item_id
   ) select count(*)::int from gone),
  1, 'the worker deletes the closed item''s code'
);
reset role;

select * from finish();
rollback;
