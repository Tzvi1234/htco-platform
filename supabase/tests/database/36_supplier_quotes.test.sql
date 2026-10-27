-- The supplier and quote model (slice 7.13): one active mapping per product (AC-7.13.1), money
-- with a currency (AC-7.13.2), the quote request's machine, immutable quotes, and row-level
-- security for members and the worker. Organization S has two shops; its Owner sees both, its
-- Support member is granted one, and organization T's Owner sees none of S's rows. The worker,
-- running a job of S, sees and writes S's rows only. A choice and a mapping agree with their quote.
-- Removing a shop or the organization removes its requests, quotes and mappings in one cascade.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(60);

-- The links to quotes and suppliers are checked at the commit, which a test never reaches: check
-- each statement at once here, and at the end, the cascades with the commit-time check forced.
set constraints app.supplier_quotes_supplier_fkey, app.quote_requests_chosen_quote_fkey,
  app.supplier_mappings_supplier_fkey, app.supplier_mappings_quote_fkey immediate;

insert into auth.users (id, email) values
  ('36e00000-0000-0000-0000-000000000001', 'owner-s@example.test'),
  ('36e00000-0000-0000-0000-000000000002', 'support-s@example.test'),
  ('36f00000-0000-0000-0000-000000000001', 'owner-t@example.test');
insert into app.organizations (id, name) values
  ('36a00000-0000-0000-0000-000000000000', 'Organization S'),
  ('36b00000-0000-0000-0000-000000000000', 'Organization T');
insert into app.memberships (id, organization_id, user_id, role) values
  ('36d00000-0000-0000-0000-000000000001', '36a00000-0000-0000-0000-000000000000', '36e00000-0000-0000-0000-000000000001', 'owner'),
  ('36d00000-0000-0000-0000-000000000002', '36a00000-0000-0000-0000-000000000000', '36e00000-0000-0000-0000-000000000002', 'support'),
  ('36d00000-0000-0000-0000-000000000003', '36b00000-0000-0000-0000-000000000000', '36f00000-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('36c00000-0000-0000-0000-000000000001', '36a00000-0000-0000-0000-000000000000', 'Shop S1', 'etsy', 'active'),
  ('36c00000-0000-0000-0000-000000000002', '36a00000-0000-0000-0000-000000000000', 'Shop S2', 'etsy', 'active'),
  ('36c00000-0000-0000-0000-000000000003', '36b00000-0000-0000-0000-000000000000', 'Shop T1', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('36d10000-0000-0000-0000-000000000002', '36a00000-0000-0000-0000-000000000000',
   '36d00000-0000-0000-0000-000000000002', '36c00000-0000-0000-0000-000000000001');

insert into app.suppliers (id, organization_id, name, autova_name) values
  ('36100000-0000-0000-0000-000000000001', '36a00000-0000-0000-0000-000000000000', 'GZ Leather', 'GZ Leather Co.'),
  ('36100000-0000-0000-0000-000000000002', '36a00000-0000-0000-0000-000000000000', 'Yiwu Craft', null),
  ('36100000-0000-0000-0000-000000000003', '36b00000-0000-0000-0000-000000000000', 'T Supplier', null);
insert into app.quote_requests (id, organization_id, shop_id, listing_id, state, updated_at) values
  ('36200000-0000-0000-0000-000000000001', '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000001', 3601, 'waiting_for_quotes', now()),
  ('36200000-0000-0000-0000-000000000002', '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000002', 3602, 'waiting_for_quotes', now()),
  ('36200000-0000-0000-0000-000000000003', '36b00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000003', 3603, 'waiting_for_quotes', now());
insert into app.supplier_quotes
  (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
   shipping_method, processing_days_min, processing_days_max, shipping_days_min,
   shipping_days_max, valid_until, source_reference, received_at)
values
  ('36300000-0000-0000-0000-000000000001', '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001', '36100000-0000-0000-0000-000000000001', 'USD', 7.40, 2.10, 'Yun Express', 2, 4, 7, 12, current_date + 14, 'mail:' || repeat('a', 64), now()),
  ('36300000-0000-0000-0000-000000000002', '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000002', '36100000-0000-0000-0000-000000000002', 'USD', 5.00, 0, 'ePacket', 1, 3, 10, 20, null, 'mail:' || repeat('b', 64), now()),
  ('36300000-0000-0000-0000-000000000003', '36b00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000003', '36100000-0000-0000-0000-000000000003', 'EUR', 3.00, 1.00, 'Post', 1, 2, 5, 9, null, 'mail:' || repeat('c', 64), now());
-- Organization T's active mapping, from its own quote.
insert into app.supplier_mappings
  (id, organization_id, shop_id, listing_id, supplier_id, quote_id, active_from)
values ('36400000-0000-0000-0000-000000000003', '36b00000-0000-0000-0000-000000000000',
        '36c00000-0000-0000-0000-000000000003', 3603, '36100000-0000-0000-0000-000000000003',
        '36300000-0000-0000-0000-000000000003', now() - interval '1 day');

-- AC-7.13.2: money has a currency; a quote never changes -----------------------------------------
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000001', null, 7, 1, 'Post', 1, 2, 3, 4, 'mail:' || repeat('d', 64), now())
$$, '23502', null, 'a quote without a currency is refused');
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000001', 'usd', 7, 1, 'Post', 1, 2, 3, 4, 'mail:' || repeat('f', 64), now())
$$, '23514', null, 'a currency is an ISO 4217 code');
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000001', 'USD', 7, 1, 'Post', 1, 2, 3, 4, 'mail:' || repeat('a', 64), now())
$$, '23505', null, 'the same mail parsed twice stores one quote');
select col_type_is('app', 'supplier_quotes', 'unit_price', 'numeric(14,4)', 'prices are exact numerics');
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000003', 'USD', 7, 1, 'Post', 1, 2, 3, 4, 'mail:' || repeat('e', 64), now())
$$, '23503', null, 'a quote cannot name another organization''s supplier');
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000001', 'USD', 7, 1, 'Post', 1, 2, 3, 4,
          'Subject: our quote for your wallet', now())
$$, '23514', null, 'a source reference is a mail hash, never mail text');
select lives_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values ('36300000-0000-0000-0000-000000000004', '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000002', 'USD', 6, 1, 'Post', 1, 2, 3, 4, 'autova-quote:Q-88213', now())
$$, 'a source reference may be AutoVA''s quote ID');
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000002', 'USD', 6, 1, 'Post', 1, 2, 3, 4,
          'autova-quote:' || repeat('a', 64), now())
$$, '23514', null, 'the two reference forms never mix');
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000001',
          '36100000-0000-0000-0000-000000000002', 'USD', 6, 1, 'Post', 1, 2, 3, 4,
          E'autova-quote:Q-88214\nUSD 6.00', now())
$$, '23514', null, 'a quote ID never carries a second line');
select throws_ok($$
  update app.supplier_quotes set unit_price = 1 where id = '36300000-0000-0000-0000-000000000001'
$$, '42501', null, 'a stored quote never changes, for any role');
select throws_ok($$
  delete from app.supplier_quotes where id = '36300000-0000-0000-0000-000000000001'
$$, '42501', null, 'a stored quote is never deleted on its own');

-- The quote request's machine ------------------------------------------------------------------
select throws_ok($$
  update app.quote_requests set state = 'mapped' where id = '36200000-0000-0000-0000-000000000001'
$$, '23514', null, 'waiting_for_quotes -> mapped is refused');
select throws_ok($$
  update app.quote_requests set state = 'chosen' where id = '36200000-0000-0000-0000-000000000002'
$$, '23514', null, 'a request cannot be chosen before any quote arrived');
update app.quote_requests set state = 'quotes_received' where id = '36200000-0000-0000-0000-000000000001';
select throws_ok($$
  update app.quote_requests set state = 'chosen' where id = '36200000-0000-0000-0000-000000000001'
$$, '23514', null, 'a choice names its quote');
select lives_ok($$
  update app.quote_requests set state = 'chosen', chosen_quote_id = '36300000-0000-0000-0000-000000000001'
  where id = '36200000-0000-0000-0000-000000000001'
$$, 'quotes_received -> chosen with its quote');
update app.quote_requests set state = 'quotes_received' where id = '36200000-0000-0000-0000-000000000002';
select throws_ok($$
  update app.quote_requests set state = 'chosen', chosen_quote_id = '36300000-0000-0000-0000-000000000001'
  where id = '36200000-0000-0000-0000-000000000002'
$$, '23503', null, 'a request cannot choose another request''s quote');
select throws_ok($$
  insert into app.quote_requests (id, organization_id, shop_id, listing_id, state, updated_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000001', 3601,
          'waiting_for_quotes', now())
$$, '23505', null, 'one open quote request per product');
select throws_ok($$
  insert into app.quote_requests (id, organization_id, shop_id, listing_id, state, updated_at)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000001', 3699,
          'mapped', now())
$$, '23514', null, 'a request starts waiting for quotes');

-- AC-7.13.1: one active mapping per product ----------------------------------------------------
insert into app.supplier_mappings
  (id, organization_id, shop_id, listing_id, supplier_id, quote_id, active_from)
values ('36400000-0000-0000-0000-000000000001', '36a00000-0000-0000-0000-000000000000',
        '36c00000-0000-0000-0000-000000000001', 3601, '36100000-0000-0000-0000-000000000001',
        '36300000-0000-0000-0000-000000000001', now() - interval '30 days');
select throws_ok($$
  insert into app.supplier_mappings (id, organization_id, shop_id, listing_id, supplier_id, active_from)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000001', 3601,
          '36100000-0000-0000-0000-000000000002', now())
$$, '23505', null, 'a second active mapping for the same product is refused');
select throws_ok($$
  update app.supplier_mappings set ended_at = now() where id = '36400000-0000-0000-0000-000000000001'
$$, '23514', null, 'a mapping ends only with its reason');
select lives_ok($$
  update app.supplier_mappings set ended_at = now(), end_reason = 'replaced'
  where id = '36400000-0000-0000-0000-000000000001'
$$, 'the old mapping ends with its reason');
select lives_ok($$
  insert into app.supplier_mappings (id, organization_id, shop_id, listing_id, supplier_id, active_from)
  values ('36400000-0000-0000-0000-000000000002', '36a00000-0000-0000-0000-000000000000',
          '36c00000-0000-0000-0000-000000000001', 3601, '36100000-0000-0000-0000-000000000002', now())
$$, 'then the new one activates');
select throws_ok($$
  update app.supplier_mappings set end_reason = 'product_retired'
  where id = '36400000-0000-0000-0000-000000000001'
$$, '23514', null, 'an ended mapping stays as it ended');
select throws_ok($$
  update app.supplier_mappings set supplier_id = '36100000-0000-0000-0000-000000000001'
  where id = '36400000-0000-0000-0000-000000000002'
$$, '23514', null, 'an active mapping changes only by ending');

-- A mapping agrees with its quote: the quote's supplier and the quote's product ------------------
select throws_ok($$
  insert into app.supplier_mappings
    (id, organization_id, shop_id, listing_id, supplier_id, quote_id, active_from)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000002', 3602,
          '36100000-0000-0000-0000-000000000001', '36300000-0000-0000-0000-000000000002', now())
$$, '23503', null, 'a mapping cannot name another supplier''s quote');
select throws_ok($$
  insert into app.supplier_mappings
    (id, organization_id, shop_id, listing_id, supplier_id, quote_id, active_from)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000002', 3699,
          '36100000-0000-0000-0000-000000000002', '36300000-0000-0000-0000-000000000002', now())
$$, '23514', null, 'a mapping cannot name a quote for another listing');
select throws_ok($$
  insert into app.supplier_mappings
    (id, organization_id, shop_id, listing_id, variant, supplier_id, quote_id, active_from)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000002', 3602,
          'red', '36100000-0000-0000-0000-000000000002', '36300000-0000-0000-0000-000000000002', now())
$$, '23514', null, 'a mapping cannot name a quote for another variant');
select throws_ok($$
  insert into app.supplier_mappings
    (id, organization_id, shop_id, listing_id, supplier_id, quote_id, active_from)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000001', 3602,
          '36100000-0000-0000-0000-000000000002', '36300000-0000-0000-0000-000000000002', now())
$$, '23514', null, 'a mapping cannot name a quote for another shop''s product');

-- Members --------------------------------------------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"36e00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select listing_id from app.quote_requests order by 1 $$,
  $$ values (3601::bigint), (3602::bigint) $$, 'Owner S: the quote requests of both S shops');
select results_eq($$ select source_reference from app.supplier_quotes order by 1 $$,
  $$ values ('autova-quote:Q-88213'), ('mail:' || repeat('a', 64)), ('mail:' || repeat('b', 64)) $$,
  'Owner S: the quotes of both S shops');
select is((select count(*)::int from app.suppliers), 2, 'Owner S: the organization''s suppliers');
select throws_ok($$ insert into app.suppliers (id, organization_id, name)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', 'By hand') $$,
  '42501', null, 'members do not write suppliers directly');
reset role;

select set_config('request.jwt.claims', '{"sub":"36e00000-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select listing_id from app.quote_requests $$,
  $$ values (3601::bigint) $$, 'Support S: the granted shop''s quote request only');
select results_eq($$ select listing_id from app.supplier_mappings order by active_from $$,
  $$ values (3601::bigint), (3601::bigint) $$, 'Support S: the granted shop''s mappings only');
reset role;

select set_config('request.jwt.claims', '{"sub":"36f00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select results_eq($$ select name from app.suppliers $$,
  $$ values ('T Supplier') $$, 'Owner T: no supplier of organization S');
select results_eq($$ select source_reference from app.supplier_quotes $$,
  $$ values ('mail:' || repeat('c', 64)) $$, 'Owner T: no quote of organization S');
reset role;

-- The worker: its job's organization only, and quotes are never updated ------------------------
select ok(not has_table_privilege('htco_worker', 'app.supplier_quotes', 'update'),
  'the worker cannot change a stored quote');
select ok(not has_table_privilege('htco_worker', 'app.supplier_mappings', 'delete'),
  'the worker cannot delete a mapping');

set local role htco_worker;
select set_config('htco.organization_id', '36a00000-0000-0000-0000-000000000000', true);
select results_eq($$ select name from app.suppliers order by 1 $$,
  $$ values ('GZ Leather'), ('Yiwu Craft') $$, 'worker for S: S''s suppliers only');
select results_eq($$ select listing_id from app.quote_requests order by 1 $$,
  $$ values (3601::bigint), (3602::bigint) $$, 'worker for S: S''s quote requests only');
select results_eq($$ select id from app.supplier_quotes order by 1 $$,
  $$ values ('36300000-0000-0000-0000-000000000001'::uuid), ('36300000-0000-0000-0000-000000000002'::uuid),
            ('36300000-0000-0000-0000-000000000004'::uuid) $$,
  'worker for S: S''s quotes only');
select results_eq($$ select id from app.supplier_mappings order by 1 $$,
  $$ values ('36400000-0000-0000-0000-000000000001'::uuid), ('36400000-0000-0000-0000-000000000002'::uuid) $$,
  'worker for S: S''s mappings only');

select throws_ok($$ insert into app.suppliers (id, organization_id, name)
  values (gen_random_uuid(), '36b00000-0000-0000-0000-000000000000', 'Planted in T') $$,
  '42501', null, 'worker for S: no supplier into T');
select throws_ok($$ insert into app.quote_requests (id, organization_id, shop_id, listing_id, state, updated_at)
  values (gen_random_uuid(), '36b00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000003', 3690,
          'waiting_for_quotes', now()) $$,
  '42501', null, 'worker for S: no quote request into T');
select throws_ok($$
  insert into app.supplier_quotes
    (id, organization_id, quote_request_id, supplier_id, currency, unit_price, shipping_cost,
     shipping_method, processing_days_min, processing_days_max, shipping_days_min,
     shipping_days_max, source_reference, received_at)
  values (gen_random_uuid(), '36b00000-0000-0000-0000-000000000000', '36200000-0000-0000-0000-000000000003',
          '36100000-0000-0000-0000-000000000003', 'EUR', 2, 1, 'Post', 1, 2, 3, 4, 'mail:' || repeat('9', 64), now())
$$, '42501', null, 'worker for S: no quote into T');
select throws_ok($$ insert into app.supplier_mappings (id, organization_id, shop_id, listing_id, supplier_id, active_from)
  values (gen_random_uuid(), '36b00000-0000-0000-0000-000000000000', '36c00000-0000-0000-0000-000000000003', 3691,
          '36100000-0000-0000-0000-000000000003', now()) $$,
  '42501', null, 'worker for S: no mapping into T');

select lives_ok($$ update app.suppliers set autova_name = 'Renamed by S'
  where id = '36100000-0000-0000-0000-000000000003' $$, 'worker for S: updating T''s supplier runs');
select lives_ok($$ update app.quote_requests set state = 'cancelled', updated_at = now()
  where id = '36200000-0000-0000-0000-000000000003' $$, 'worker for S: updating T''s request runs');
select lives_ok($$ update app.supplier_mappings set ended_at = now(), end_reason = 'product_retired'
  where id = '36400000-0000-0000-0000-000000000003' $$, 'worker for S: updating T''s mapping runs');
select lives_ok($$ insert into app.suppliers (id, organization_id, name)
  values (gen_random_uuid(), '36a00000-0000-0000-0000-000000000000', 'Found by the parser') $$,
  'worker for S: a supplier into S is fine');
reset role;

select is((select autova_name from app.suppliers where id = '36100000-0000-0000-0000-000000000003'),
  null, 'T''s supplier is unchanged by S''s worker');
select is((select state from app.quote_requests where id = '36200000-0000-0000-0000-000000000003'),
  'waiting_for_quotes', 'T''s request is unchanged by S''s worker');
select is((select ended_at from app.supplier_mappings where id = '36400000-0000-0000-0000-000000000003'),
  null, 'T''s mapping is unchanged by S''s worker');

-- Removing a shop, then the organization, removes the requests, quotes and mappings ------------
-- Shop S2: a chosen request, its quote, and an active mapping from that quote.
update app.quote_requests set state = 'chosen', chosen_quote_id = '36300000-0000-0000-0000-000000000002'
  where id = '36200000-0000-0000-0000-000000000002';
insert into app.supplier_mappings
  (id, organization_id, shop_id, listing_id, supplier_id, quote_id, active_from)
values ('36400000-0000-0000-0000-000000000004', '36a00000-0000-0000-0000-000000000000',
        '36c00000-0000-0000-0000-000000000002', 3602, '36100000-0000-0000-0000-000000000002',
        '36300000-0000-0000-0000-000000000002', now());
set constraints app.supplier_quotes_supplier_fkey, app.quote_requests_chosen_quote_fkey,
  app.supplier_mappings_supplier_fkey, app.supplier_mappings_quote_fkey deferred;

select lives_ok($$ delete from app.shops where id = '36c00000-0000-0000-0000-000000000002' $$,
  'removing a shop with a chosen request, its quote and a mapping from it works');
select lives_ok($$ set constraints all immediate $$,
  'and the links checked at the commit still hold');
select is(
  (select count(*)::int from app.quote_requests where shop_id = '36c00000-0000-0000-0000-000000000002')
  + (select count(*)::int from app.supplier_quotes where id = '36300000-0000-0000-0000-000000000002')
  + (select count(*)::int from app.supplier_mappings where shop_id = '36c00000-0000-0000-0000-000000000002'),
  0, 'the shop''s requests, quotes and mappings are gone');

set constraints app.supplier_quotes_supplier_fkey, app.quote_requests_chosen_quote_fkey,
  app.supplier_mappings_supplier_fkey, app.supplier_mappings_quote_fkey deferred;
select lives_ok($$ delete from app.organizations where id = '36a00000-0000-0000-0000-000000000000' $$,
  'removing an organization with suppliers, a chosen request, its quotes and mappings works');
select lives_ok($$ set constraints all immediate $$,
  'and the links checked at the commit still hold');
select is(
  (select count(*)::int from app.suppliers where organization_id = '36a00000-0000-0000-0000-000000000000')
  + (select count(*)::int from app.quote_requests where organization_id = '36a00000-0000-0000-0000-000000000000')
  + (select count(*)::int from app.supplier_quotes where organization_id = '36a00000-0000-0000-0000-000000000000')
  + (select count(*)::int from app.supplier_mappings where organization_id = '36a00000-0000-0000-0000-000000000000'),
  0, 'the organization''s suppliers, requests, quotes and mappings are gone');
select is(
  (select count(*)::int from app.suppliers where organization_id = '36b00000-0000-0000-0000-000000000000')
  + (select count(*)::int from app.quote_requests where organization_id = '36b00000-0000-0000-0000-000000000000')
  + (select count(*)::int from app.supplier_quotes where organization_id = '36b00000-0000-0000-0000-000000000000')
  + (select count(*)::int from app.supplier_mappings where organization_id = '36b00000-0000-0000-0000-000000000000'),
  4, 'organization T keeps its rows');

select * from finish();
rollback;
