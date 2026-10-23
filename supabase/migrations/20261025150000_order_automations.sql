-- The order automations (slice 5.11): Product Sales Counters' tables, and what Order
-- Deduplication & Verification records.
--
-- `app.product_sales_counters`: per shop and listing, the units and orders that count as sales
-- (paid, never cancelled, refunded or unpaid; FR-ORD-03's `counts_as_sale`). Numbers only.
--
-- `app.product_sales_applications`: per receipt, what the counters hold for it now (listing →
-- units). Each run moves the counters by the difference between what the receipt should count
-- and what it counted before, so a replayed cancellation changes nothing the second time.
--
-- `app.sync_drifts` gains the kind `totals` (the stored grand total differs from Etsy's; the
-- values say which field, never an amount) and the source `verification`. The worker still reads
-- no money column: `private.order_grand_total_matches` answers only whether the stored total
-- equals the one it is given, for its job's organization.
--
-- Safe on the live database: two new tables, two widened check constraints and one function.

create table app.product_sales_counters (
  organization_id uuid not null,
  shop_id uuid not null,
  listing_id bigint not null constraint product_sales_counters_listing_id_check check (listing_id > 0),
  units bigint not null default 0 constraint product_sales_counters_units_check check (units >= 0),
  orders bigint not null default 0 constraint product_sales_counters_orders_check check (orders >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (shop_id, listing_id),
  constraint product_sales_counters_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
);
create index product_sales_counters_organization_id_idx
  on app.product_sales_counters (organization_id);
create trigger product_sales_counters_set_updated_at before update on app.product_sales_counters
  for each row execute function private.set_updated_at();

create table app.product_sales_applications (
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  -- {"<listing_id>": units}: empty when the receipt counts nothing (unpaid, cancelled, refunded).
  counted jsonb not null default '{}'::jsonb constraint product_sales_applications_counted_check
    check (jsonb_typeof(counted) = 'object' and pg_column_size(counted) <= 8192),
  applied_at timestamptz not null,
  execution_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (shop_id, receipt_id),
  constraint product_sales_applications_order_fkey foreign key (shop_id, receipt_id)
    references app.etsy_orders (shop_id, receipt_id) on delete cascade
);
create index product_sales_applications_organization_id_idx
  on app.product_sales_applications (organization_id);
create trigger product_sales_applications_set_updated_at
  before update on app.product_sales_applications
  for each row execute function private.set_updated_at();

alter table app.product_sales_counters enable row level security;
alter table app.product_sales_applications enable row level security;
revoke all on app.product_sales_counters, app.product_sales_applications
  from public, anon, authenticated, service_role;

-- Members read their shops' counters (numbers only, like the statistics).
grant select on app.product_sales_counters to authenticated;
create policy product_sales_counters_select on app.product_sales_counters for select
  to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );

-- The worker: its job's organization only.
grant select, insert, update on app.product_sales_counters, app.product_sales_applications
  to htco_worker;
create policy product_sales_counters_worker on app.product_sales_counters for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
create policy product_sales_applications_worker on app.product_sales_applications for all
  to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- Order Deduplication & Verification's drifts.
alter table app.sync_drifts drop constraint sync_drifts_kind_check;
alter table app.sync_drifts add constraint sync_drifts_kind_check check (
  kind in ('missing', 'status', 'items', 'shipments', 'totals')
);
alter table app.sync_drifts drop constraint sync_drifts_source_check;
alter table app.sync_drifts add constraint sync_drifts_source_check check (
  source in ('reconciliation', 'webhook', 'backfill', 'sync', 'verification')
);

-- Whether the stored grand total equals Etsy's, without the worker reading the amount. Null when
-- the order is not stored in the worker's organization.
create function private.order_grand_total_matches(
  p_shop_id uuid, p_receipt_id bigint, p_amount bigint, p_divisor integer, p_currency text
) returns boolean
language sql stable security definer set search_path = '' as $$
  select o.grand_total_amount = p_amount
     and o.grand_total_divisor = p_divisor
     and o.grand_total_currency = p_currency
  from app.etsy_orders o
  where o.shop_id = p_shop_id
    and o.receipt_id = p_receipt_id
    and o.organization_id = (select private.worker_organization_id())
$$;
revoke all on function private.order_grand_total_matches(uuid, bigint, bigint, integer, text)
  from public, anon, authenticated, service_role;
grant execute on function private.order_grand_total_matches(uuid, bigint, bigint, integer, text)
  to htco_worker;
