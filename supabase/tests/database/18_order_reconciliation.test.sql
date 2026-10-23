-- Reconciliation's tables (slice 5.5): drifts recorded once per receipt, kind and Etsy update,
-- cursors per shop, both reachable by the worker in its job's organization only.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(9);

insert into app.organizations (id, name) values
  ('8a000000-0000-0000-0000-000000000000', 'Organization R'),
  ('8b000000-0000-0000-0000-000000000000', 'Organization S');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('8c000000-0000-0000-0000-000000000001', '8a000000-0000-0000-0000-000000000000', 'Shop R', 'etsy', 'active'),
  ('8d000000-0000-0000-0000-000000000001', '8b000000-0000-0000-0000-000000000000', 'Shop S', 'etsy', 'active');

select ok(not has_table_privilege('authenticated', 'app.sync_drifts', 'insert, update, delete'),
  'members never write drifts');
select ok(not has_table_privilege('anon', 'app.sync_drifts', 'select'), 'anon reads no drift');
select ok(has_table_privilege('htco_worker', 'app.sync_cursors', 'insert, update'),
  'the worker keeps the cursors');

set local role htco_worker;
select set_config('htco.organization_id', '8a000000-0000-0000-0000-000000000000', true);

select lives_ok(
  $$insert into app.sync_drifts
      (id, organization_id, shop_id, receipt_id, kind, stored, observed, etsy_updated_at, source,
       detected_at)
    values (gen_random_uuid(), '8a000000-0000-0000-0000-000000000000',
            '8c000000-0000-0000-0000-000000000001', 8001, 'status', '{"status": "paid"}',
            '{"status": "completed"}', '2026-10-24 08:00+00', 'reconciliation', now())$$,
  'a drift of the job''s organization is recorded'
);
select throws_ok(
  $$insert into app.sync_drifts
      (id, organization_id, shop_id, receipt_id, kind, stored, observed, etsy_updated_at, source,
       detected_at)
    values (gen_random_uuid(), '8a000000-0000-0000-0000-000000000000',
            '8c000000-0000-0000-0000-000000000001', 8001, 'status', '{"status": "paid"}',
            '{"status": "completed"}', '2026-10-24 08:00+00', 'reconciliation', now())$$,
  '23505', null, 'the same drift is recorded once'
);
select throws_ok(
  $$insert into app.sync_drifts
      (id, organization_id, shop_id, receipt_id, kind, observed, etsy_updated_at, source,
       detected_at)
    values (gen_random_uuid(), '8a000000-0000-0000-0000-000000000000',
            '8c000000-0000-0000-0000-000000000001', 8002, 'price', '{}', now(), 'reconciliation',
            now())$$,
  '23514', null, 'only the documented kinds exist'
);
select throws_ok(
  $$insert into app.sync_drifts
      (id, organization_id, shop_id, receipt_id, kind, observed, etsy_updated_at, source,
       detected_at)
    values (gen_random_uuid(), '8b000000-0000-0000-0000-000000000000',
            '8d000000-0000-0000-0000-000000000001', 8003, 'missing', '{}', now(),
            'reconciliation', now())$$,
  '42501', null, 'no drift of another organization'
);
select lives_ok(
  $$insert into app.sync_cursors (organization_id, shop_id, resource, kind, last_started_at,
                                  last_succeeded_at)
    values ('8a000000-0000-0000-0000-000000000000', '8c000000-0000-0000-0000-000000000001',
            'orders', 'reconcile', now(), now())$$,
  'the worker records its cursor'
);
select throws_ok(
  $$update app.sync_cursors set last_error = 'a message with "quotes" and more'
    where shop_id = '8c000000-0000-0000-0000-000000000001'$$,
  '23514', null, 'a cursor keeps an error type, never a message'
);

reset role;
select * from finish();
rollback;
