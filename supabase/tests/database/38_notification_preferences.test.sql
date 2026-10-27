-- Notification preferences per severity and per shop (slice 7.6): each user's own rows, a mute
-- only for a shop the user can see, and the worker reads them in its job's organization only.
begin;
create extension if not exists pgtap with schema extensions;
select plan(12);

insert into auth.users (id, email) values
  ('37e00000-0000-0000-0000-000000000001', 'owner-p@example.test'),
  ('37e00000-0000-0000-0000-000000000002', 'support-p@example.test'),
  ('37f00000-0000-0000-0000-000000000001', 'owner-q@example.test');
insert into app.organizations (id, name) values
  ('37a00000-0000-0000-0000-000000000000', 'Organization P'),
  ('37b00000-0000-0000-0000-000000000000', 'Organization Q');
insert into app.memberships (id, organization_id, user_id, role) values
  ('37d00000-0000-0000-0000-000000000001', '37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000001', 'owner'),
  ('37d00000-0000-0000-0000-000000000002', '37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000002', 'support'),
  ('37d00000-0000-0000-0000-000000000003', '37b00000-0000-0000-0000-000000000000', '37f00000-0000-0000-0000-000000000001', 'owner');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('37c00000-0000-0000-0000-000000000001', '37a00000-0000-0000-0000-000000000000', 'Shop P1', 'etsy', 'active'),
  ('37c00000-0000-0000-0000-000000000002', '37a00000-0000-0000-0000-000000000000', 'Shop P2', 'etsy', 'active'),
  ('37c00000-0000-0000-0000-000000000003', '37b00000-0000-0000-0000-000000000000', 'Shop Q1', 'etsy', 'active');
insert into app.shop_access (id, organization_id, membership_id, shop_id) values
  ('37d10000-0000-0000-0000-000000000002', '37a00000-0000-0000-0000-000000000000',
   '37d00000-0000-0000-0000-000000000002', '37c00000-0000-0000-0000-000000000001');
insert into app.notification_severity_preferences (organization_id, user_id, severity, in_app, email)
values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000002', 'low', true, false);

select throws_ok($$
  insert into app.notification_severity_preferences (organization_id, user_id, severity)
  values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000001', 'urgent')
$$, '23514', null, 'a severity is low, medium, high or critical');
select throws_ok($$
  insert into app.notification_shop_mutes (organization_id, user_id, shop_id)
  values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000001',
          '37c00000-0000-0000-0000-000000000003')
$$, '23503', null, 'a mute names a shop of the same organization');

-- The Owner of P mutes Shop P2 and turns off low email.
select set_config('request.jwt.claims', '{"sub":"37e00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select lives_ok($$
  insert into app.notification_shop_mutes (organization_id, user_id, shop_id)
  values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000001',
          '37c00000-0000-0000-0000-000000000002')
$$, 'an Owner mutes one of the organization''s shops');
select lives_ok($$
  insert into app.notification_severity_preferences (organization_id, user_id, severity, in_app, email)
  values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000001', 'low', true, false)
$$, 'and chooses the channels for a severity');
select is((select count(*)::int from app.notification_severity_preferences), 1,
  'a user sees only their own severity choices');
select throws_ok($$
  insert into app.notification_shop_mutes (organization_id, user_id, shop_id)
  values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000002',
          '37c00000-0000-0000-0000-000000000001')
$$, '42501', null, 'nobody writes another user''s mutes');
reset role;

-- Support P: granted Shop P1 only.
select set_config('request.jwt.claims', '{"sub":"37e00000-0000-0000-0000-000000000002","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select lives_ok($$
  insert into app.notification_shop_mutes (organization_id, user_id, shop_id)
  values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000002',
          '37c00000-0000-0000-0000-000000000001')
$$, 'Support mutes the granted shop');
select throws_ok($$
  insert into app.notification_shop_mutes (organization_id, user_id, shop_id)
  values ('37a00000-0000-0000-0000-000000000000', '37e00000-0000-0000-0000-000000000002',
          '37c00000-0000-0000-0000-000000000002')
$$, '42501', null, 'but not a shop it cannot see');
select results_eq($$ select severity from app.notification_severity_preferences $$,
  $$ values ('low') $$, 'Support sees its own severity choice');
reset role;

-- Owner Q: nothing of organization P.
select set_config('request.jwt.claims', '{"sub":"37f00000-0000-0000-0000-000000000001","role":"authenticated","aal":"aal2"}', true);
set local role authenticated;
select is((select count(*)::int from app.notification_shop_mutes), 0, 'Owner Q: no mute of P');
reset role;

-- The worker reads, never writes.
select ok(has_table_privilege('htco_worker', 'app.notification_shop_mutes', 'select')
  and not has_table_privilege('htco_worker', 'app.notification_shop_mutes', 'insert'),
  'the worker reads the mutes and writes none');
select ok(not has_table_privilege('htco_worker', 'app.notification_severity_preferences', 'update'),
  'the worker never changes a severity choice');

select * from finish();
rollback;
