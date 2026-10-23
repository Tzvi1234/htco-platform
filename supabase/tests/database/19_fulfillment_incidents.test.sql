-- Fulfillment monitoring and incidents (slices 6.1 to 6.3): one tracking row per parcel, one open
-- incident per key, the incident machine and the evidence rule in the database, append-only
-- events, RLS for members, and the worker's least privilege in its job's organization.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(21);

insert into auth.users (id, email) values
  ('91111111-0000-0000-0000-000000000001', 'owner-k@example.test'),
  ('92222222-0000-0000-0000-000000000001', 'owner-l@example.test');
insert into app.organizations (id, name) values
  ('9a000000-0000-0000-0000-000000000000', 'Organization K'),
  ('9b000000-0000-0000-0000-000000000000', 'Organization L');
insert into app.memberships (id, organization_id, user_id, role) values
  ('9e000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', '91111111-0000-0000-0000-000000000001', 'owner'),
  ('9f000000-0000-0000-0000-000000000001', '9b000000-0000-0000-0000-000000000000', '92222222-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('9c000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000', 'Shop K', 'etsy', 'active'),
  ('9d000000-0000-0000-0000-000000000001', '9b000000-0000-0000-0000-000000000000', 'Shop L', 'etsy', 'active');
insert into app.etsy_orders
  (organization_id, shop_id, etsy_shop_id, receipt_id, status, is_paid, is_shipped,
   grand_total_amount, grand_total_divisor, grand_total_currency,
   subtotal_amount, subtotal_divisor, subtotal_currency,
   shipping_amount, shipping_divisor, shipping_currency,
   discount_amount, discount_divisor, discount_currency,
   etsy_created_at, etsy_updated_at, synced_at, order_state)
values
  ('9a000000-0000-0000-0000-000000000000', '9c000000-0000-0000-0000-000000000001', 901, 9001,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now(), 'awaiting_shipment'),
  ('9b000000-0000-0000-0000-000000000000', '9d000000-0000-0000-0000-000000000001', 902, 9002,
   'paid', true, false, 100, 100, 'USD', 100, 100, 'USD', 0, 100, 'USD', 0, 100, 'USD',
   now(), now(), now(), 'awaiting_shipment');
insert into app.tracking_events
  (id, organization_id, shop_id, receipt_id, carrier, carrier_name, fingerprint, last4, source,
   observed_via, first_observed_at)
values
  ('97000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000',
   '9c000000-0000-0000-0000-000000000001', 9001, 'usps', 'USPS', decode(repeat('a1', 32), 'hex'),
   'AB12', 'etsy', 'webhook', now()),
  ('97000000-0000-0000-0000-000000000002', '9b000000-0000-0000-0000-000000000000',
   '9d000000-0000-0000-0000-000000000001', 9002, 'usps', 'USPS', decode(repeat('a2', 32), 'hex'),
   'CD34', 'etsy', 'webhook', now());
insert into app.incidents
  (id, organization_id, shop_id, receipt_id, dedup_key, cause, resource_kind, resource_id,
   severity, state, source, detected_at, updated_at)
values
  ('98000000-0000-0000-0000-000000000001', '9a000000-0000-0000-0000-000000000000',
   '9c000000-0000-0000-0000-000000000001', 9001, 'tracking_missing:order:k/9001',
   'tracking_missing', 'order', 'k/9001', 'medium', 'detected', 'sla_inspector', now(), now()),
  ('98000000-0000-0000-0000-000000000002', '9b000000-0000-0000-0000-000000000000',
   '9d000000-0000-0000-0000-000000000001', 9002, 'tracking_missing:order:l/9002',
   'tracking_missing', 'order', 'l/9002', 'medium', 'detected', 'sla_inspector', now(), now());
insert into app.incident_events
  (id, organization_id, incident_id, from_state, to_state, actor_kind, actor_id, occurred_at)
values
  (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
   '98000000-0000-0000-0000-000000000001', null, 'detected', 'automation',
   'incident-detection-escalation', now());

-- One row per parcel; one open incident per key ----------------------------------------------
select throws_ok(
  $$insert into app.tracking_events
      (id, organization_id, shop_id, receipt_id, carrier, carrier_name, fingerprint, last4,
       source, observed_via, first_observed_at)
    values (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
            '9c000000-0000-0000-0000-000000000001', 9001, 'usps', 'USPS',
            decode(repeat('a1', 32), 'hex'), 'AB12', 'etsy', 'schedule', now())$$,
  '23505', null, 'a parcel is recorded once per order'
);
select throws_ok(
  $$insert into app.tracking_events
      (id, organization_id, shop_id, receipt_id, carrier, carrier_name, fingerprint, last4,
       source, observed_via, first_observed_at)
    values (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
            '9c000000-0000-0000-0000-000000000001', 9001, 'usps', 'USPS',
            decode(repeat('a3', 32), 'hex'), 'LX123456789CN', 'etsy', 'schedule', now())$$,
  '23514', null, 'only the last four characters of a code are stored'
);
select throws_ok(
  $$insert into app.incidents
      (id, organization_id, shop_id, receipt_id, dedup_key, cause, resource_kind, resource_id,
       severity, state, source, detected_at, updated_at)
    values (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
            '9c000000-0000-0000-0000-000000000001', 9001, 'tracking_missing:order:k/9001',
            'tracking_missing', 'order', 'k/9001', 'medium', 'detected', 'sla_inspector',
            now(), now())$$,
  '23505', null, 'a second open incident with the same key is refused'
);

-- The machine and the evidence rule -----------------------------------------------------------
select throws_ok(
  $$update app.incidents set state = 'approval_required'
    where id = '98000000-0000-0000-0000-000000000001'$$,
  '23514', null, 'an illegal step of the incident is refused'
);
select throws_ok(
  $$update app.incidents set state = 'resolved', resolved_at = now()
    where id = '98000000-0000-0000-0000-000000000001'$$,
  '23514', null, 'an incident never resolves without evidence'
);
select throws_ok(
  $$insert into app.incident_events
      (id, organization_id, incident_id, from_state, to_state, actor_kind, actor_id, occurred_at)
    values (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
            '98000000-0000-0000-0000-000000000001', 'verifying', 'resolved', 'user', 'u', now())$$,
  '23514', null, 'a resolution event carries its evidence'
);
select throws_ok(
  $$insert into app.incident_events
      (id, organization_id, incident_id, from_state, to_state, actor_kind, actor_id, occurred_at)
    values (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
            '98000000-0000-0000-0000-000000000001', 'detected', 'resolved', 'user', 'u', now())$$,
  '23514', null, 'an event outside the machine is refused'
);
select lives_ok(
  $$insert into app.incident_events
      (id, organization_id, incident_id, from_state, to_state, actor_kind, actor_id, occurred_at,
       evidence_kind, evidence_source, evidence_reference, evidence_observed_at)
    values
      (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
       '98000000-0000-0000-0000-000000000001', 'detected', 'verifying', 'automation', 'tracking-sync',
       now(), 'tracking_present', 'tracking:etsy', '97000000-0000-0000-0000-000000000001', now()),
      (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
       '98000000-0000-0000-0000-000000000001', 'verifying', 'resolved', 'automation', 'tracking-sync',
       now(), 'tracking_present', 'tracking:etsy', '97000000-0000-0000-0000-000000000001', now());
    update app.incidents set state = 'resolved', resolved_at = now()
    where id = '98000000-0000-0000-0000-000000000001'$$,
  'with its evidence recorded, the incident resolves'
);
select lives_ok(
  $$insert into app.incidents
      (id, organization_id, shop_id, receipt_id, dedup_key, cause, resource_kind, resource_id,
       severity, state, source, detected_at, updated_at)
    values ('98000000-0000-0000-0000-000000000003', '9a000000-0000-0000-0000-000000000000',
            '9c000000-0000-0000-0000-000000000001', 9001, 'tracking_missing:order:k/9001',
            'tracking_missing', 'order', 'k/9001', 'medium', 'detected', 'sla_inspector',
            now(), now())$$,
  'after the resolution, the same key may open a new incident'
);
select throws_ok(
  $$delete from app.incident_events where incident_id = '98000000-0000-0000-0000-000000000001'$$,
  '42501', null, 'incident events are append-only'
);

select throws_ok(
  $$insert into app.sla_policies (id, organization_id, shop_id, supplier, window_days, unit)
    values (gen_random_uuid(), '9a000000-0000-0000-0000-000000000000',
            '9c000000-0000-0000-0000-000000000001', 'yun', 5, 'business_days')$$,
  '23514', null, 'no supplier or shipping-method policy until orders carry them (Phase 13)'
);

-- Members see their own organization's rows ---------------------------------------------------
select set_config('request.jwt.claims', '{"sub":"91111111-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}', true);
set local role authenticated;
select is((select count(*)::integer from app.incidents), 2, 'the owner sees the incidents of the organization');
select is((select count(*)::integer from app.tracking_events), 1, 'and only its tracking events');
select is((select count(*)::integer from app.incident_events), 3, 'and the events of its incidents');
select throws_ok(
  $$update app.incidents set state = 'triaged' where id = '98000000-0000-0000-0000-000000000003'$$,
  '42501', null, 'members do not write incidents directly (the API does, Phase 6.5)'
);
reset role;

-- The worker: its job's organization, least privilege ----------------------------------------
select ok(not has_column_privilege('htco_worker', 'app.incidents', 'details', 'update'),
  'the worker cannot rewrite what an incident found');
select ok(has_column_privilege('htco_worker', 'app.etsy_orders', 'etsy_created_at', 'select'),
  'the worker reads when an order was placed');
select ok(not has_column_privilege('htco_worker', 'app.etsy_order_buyers', 'shipments', 'select'),
  'the worker still cannot read the full tracking codes');

set local role htco_worker;
select set_config('htco.organization_id', '9a000000-0000-0000-0000-000000000000', true);
select is((select count(*)::integer from app.incidents), 2, 'the worker sees its organization''s incidents');
select is(
  (select count(*)::integer from app.tracking_events where receipt_id = 9002), 0,
  'the worker sees no tracking of another organization'
);
select throws_ok(
  $$insert into app.incidents
      (id, organization_id, shop_id, receipt_id, dedup_key, cause, resource_kind, resource_id,
       severity, state, source, detected_at, updated_at)
    values (gen_random_uuid(), '9b000000-0000-0000-0000-000000000000',
            '9d000000-0000-0000-0000-000000000001', 9002, 'sync_drift:order:l/9002',
            'sync_drift', 'order', 'l/9002', 'low', 'detected', 'reconciliation', now(), now())$$,
  '42501', null, 'the worker opens no incident in another organization'
);

reset role;
select * from finish();
rollback;
