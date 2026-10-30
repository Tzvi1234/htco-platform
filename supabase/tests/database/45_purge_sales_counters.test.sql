-- The disconnect purge may delete the product sales counters (Codex post-merge review, owner
-- gate v4): the worker deletes its job's organization's counters only, and users still cannot.
begin;
create extension if not exists pgtap with schema extensions;
grant usage on schema extensions to htco_worker;
select plan(4);

insert into app.organizations (id, name) values
  ('45a00000-0000-0000-0000-000000000000', 'Organization P'),
  ('45b00000-0000-0000-0000-000000000000', 'Organization Q');
insert into app.shops (id, organization_id, name, marketplace, status) values
  ('45c00000-0000-0000-0000-000000000001', '45a00000-0000-0000-0000-000000000000', 'Shop P', 'etsy', 'active'),
  ('45c00000-0000-0000-0000-000000000002', '45b00000-0000-0000-0000-000000000000', 'Shop Q', 'etsy', 'active');
insert into app.product_sales_counters (organization_id, shop_id, listing_id, units, orders) values
  ('45a00000-0000-0000-0000-000000000000', '45c00000-0000-0000-0000-000000000001', 7, 10, 5),
  ('45b00000-0000-0000-0000-000000000000', '45c00000-0000-0000-0000-000000000002', 7, 3, 1);

select ok(has_table_privilege('htco_worker', 'app.product_sales_counters', 'delete'),
  'the worker may delete counters (the purge)');
select ok(not has_table_privilege('authenticated', 'app.product_sales_counters', 'delete'),
  'users still cannot delete counters');

set local role htco_worker;
select set_config('htco.organization_id', '45a00000-0000-0000-0000-000000000000', true);
delete from app.product_sales_counters
  where organization_id = '45b00000-0000-0000-0000-000000000000';
delete from app.product_sales_counters
  where organization_id = '45a00000-0000-0000-0000-000000000000'
    and shop_id = '45c00000-0000-0000-0000-000000000001';
reset role;

select is((select count(*)::int from app.product_sales_counters
           where organization_id = '45a00000-0000-0000-0000-000000000000'), 0,
  'the worker deletes its job''s organization''s counters');
select is((select count(*)::int from app.product_sales_counters
           where organization_id = '45b00000-0000-0000-0000-000000000000'), 1,
  'and never another organization''s');

select * from finish();
rollback;
