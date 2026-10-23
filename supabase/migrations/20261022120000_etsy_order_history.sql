-- The shop's stored order history (owner's decision of 2026-10-22).
--
-- One row per Etsy receipt (`app.etsy_orders`), its items, its refunds with Etsy's reason, and in
-- a table of its own the buyer's name, shipping address and tracking numbers
-- (`app.etsy_order_buyers`). `app.etsy_order_sync_state` holds the backfill's checkpoint and
-- the time of the last sync. Contact data is never stored: there is no column for an email
-- address, a phone number, a message, a gift message or a note from the buyer.
--
-- Only the API's login role writes. Members read the orders, items, refunds and sync state of
-- the shops they may see; the buyer table only roles that may see buyer details (Owner and
-- Admin of the organization, Operations and Support with access to the shop). The API applies
-- the same rule before it reads the buyer table.
--
-- A disconnected shop's stored orders are deleted once its `purge_after` date has passed
-- (`private.purge_disconnected_etsy_orders`, run by the Phase 5 worker).

-- Owner and Admin see buyer data of every shop of their organization; Operations and Support of
-- the shops they were granted (the permission matrix's `view_buyer_contact`).
create function private.user_buyer_data_shop_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select s.id
  from app.memberships m
  join app.shops s on s.organization_id = m.organization_id
  where m.user_id = (select auth.uid()) and m.revoked_at is null and m.role in ('owner', 'admin')
  union
  select sa.shop_id
  from app.shop_access sa
  join app.memberships m on m.id = sa.membership_id
  where m.user_id = (select auth.uid()) and m.revoked_at is null
    and m.role in ('operations', 'support')
$$;
revoke all on function private.user_buyer_data_shop_ids() from public, anon, authenticated;
grant execute on function private.user_buyer_data_shop_ids() to authenticated;

create table app.etsy_orders (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null,
  shop_id uuid not null,
  etsy_shop_id bigint not null constraint etsy_orders_etsy_shop_id_check check (etsy_shop_id > 0),
  receipt_id bigint not null constraint etsy_orders_receipt_id_check check (receipt_id > 0),
  -- Etsy's status, lower case ("paid", "completed", "canceled", "fully refunded", ...).
  status text not null constraint etsy_orders_status_check check (status ~ '^[a-z ]{1,32}$'),
  is_paid boolean not null,
  is_shipped boolean not null,
  -- Money exactly as Etsy sends it: amount / divisor in the currency.
  grand_total_amount bigint not null,
  grand_total_divisor integer not null constraint etsy_orders_grand_total_divisor_check check (grand_total_divisor > 0),
  grand_total_currency text not null constraint etsy_orders_grand_total_currency_check check (grand_total_currency ~ '^[A-Z]{3}$'),
  subtotal_amount bigint not null,
  subtotal_divisor integer not null constraint etsy_orders_subtotal_divisor_check check (subtotal_divisor > 0),
  subtotal_currency text not null constraint etsy_orders_subtotal_currency_check check (subtotal_currency ~ '^[A-Z]{3}$'),
  shipping_amount bigint not null,
  shipping_divisor integer not null constraint etsy_orders_shipping_divisor_check check (shipping_divisor > 0),
  shipping_currency text not null constraint etsy_orders_shipping_currency_check check (shipping_currency ~ '^[A-Z]{3}$'),
  discount_amount bigint not null,
  discount_divisor integer not null constraint etsy_orders_discount_divisor_check check (discount_divisor > 0),
  discount_currency text not null constraint etsy_orders_discount_currency_check check (discount_currency ~ '^[A-Z]{3}$'),
  -- The destination country only (aggregate statistics); the address is in etsy_order_buyers.
  country_code text constraint etsy_orders_country_code_check check (country_code ~ '^[A-Z]{2}$'),
  -- Carrier and time of each shipment; tracking numbers are buyer data (etsy_order_buyers).
  shipments jsonb not null default '[]' constraint etsy_orders_shipments_check check (jsonb_typeof(shipments) = 'array'),
  etsy_created_at timestamptz not null,
  etsy_updated_at timestamptz not null,
  synced_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint etsy_orders_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint etsy_orders_shop_id_receipt_id_key unique (shop_id, receipt_id)
);
create index etsy_orders_organization_id_idx on app.etsy_orders (organization_id);
create index etsy_orders_shop_created_idx on app.etsy_orders (shop_id, etsy_created_at desc);

create table app.etsy_order_items (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  transaction_id bigint not null constraint etsy_order_items_transaction_id_check check (transaction_id > 0),
  position integer not null constraint etsy_order_items_position_check check (position >= 0),
  listing_id bigint,
  title text not null,
  sku text,
  quantity integer not null constraint etsy_order_items_quantity_check check (quantity >= 0),
  unit_price_amount bigint not null,
  unit_price_divisor integer not null constraint etsy_order_items_unit_price_divisor_check check (unit_price_divisor > 0),
  unit_price_currency text not null constraint etsy_order_items_unit_price_currency_check check (unit_price_currency ~ '^[A-Z]{3}$'),
  is_digital boolean not null,
  expected_ship_date timestamptz,
  constraint etsy_order_items_order_fkey foreign key (shop_id, receipt_id)
    references app.etsy_orders (shop_id, receipt_id) on delete cascade,
  constraint etsy_order_items_shop_id_receipt_id_position_key unique (shop_id, receipt_id, position)
);
create index etsy_order_items_organization_id_idx on app.etsy_order_items (organization_id);

-- Refunds with Etsy's reason, keyed by their place on the receipt (oldest first); replaced as a
-- whole whenever the receipt changes, so a replay never duplicates one. `note_from_issuer` is the
-- seller's own note: stored, never logged.
create table app.etsy_order_refunds (
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  position integer not null constraint etsy_order_refunds_position_check check (position >= 0),
  amount bigint not null,
  divisor integer not null constraint etsy_order_refunds_divisor_check check (divisor > 0),
  currency text not null constraint etsy_order_refunds_currency_check check (currency ~ '^[A-Z]{3}$'),
  refunded_at timestamptz not null,
  reason text,
  status text,
  note_from_issuer text,
  primary key (shop_id, receipt_id, position),
  constraint etsy_order_refunds_order_fkey foreign key (shop_id, receipt_id)
    references app.etsy_orders (shop_id, receipt_id) on delete cascade
);
create index etsy_order_refunds_organization_id_idx on app.etsy_order_refunds (organization_id);

-- Personal data: the buyer's name, shipping address and tracking numbers. No email, phone,
-- message or note column exists.
create table app.etsy_order_buyers (
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  buyer_name text,
  address_first_line text,
  address_second_line text,
  address_city text,
  address_state text,
  address_postal_code text,
  address_country_code text constraint etsy_order_buyers_country_code_check check (address_country_code ~ '^[A-Z]{2}$'),
  -- Each shipment with its tracking number, in the order of etsy_orders.shipments.
  shipments jsonb not null default '[]' constraint etsy_order_buyers_shipments_check check (jsonb_typeof(shipments) = 'array'),
  primary key (shop_id, receipt_id),
  constraint etsy_order_buyers_order_fkey foreign key (shop_id, receipt_id)
    references app.etsy_orders (shop_id, receipt_id) on delete cascade
);
create index etsy_order_buyers_organization_id_idx on app.etsy_order_buyers (organization_id);

create table app.etsy_order_sync_state (
  shop_id uuid primary key,
  organization_id uuid not null,
  etsy_shop_id bigint not null constraint etsy_order_sync_state_etsy_shop_id_check check (etsy_shop_id > 0),
  -- The backfill's checkpoint: the offset of the next page of receipts (newest first).
  backfill_offset integer not null default 0 constraint etsy_order_sync_state_backfill_offset_check check (backfill_offset >= 0),
  backfill_completed_at timestamptz,
  incremental_synced_at timestamptz,
  last_attempt_at timestamptz,
  -- An exception type name, never a message (messages can quote buyer data).
  last_error text constraint etsy_order_sync_state_last_error_check check (last_error ~ '^[A-Za-z0-9_.]{1,64}$'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint etsy_order_sync_state_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
);
create index etsy_order_sync_state_organization_id_idx on app.etsy_order_sync_state (organization_id);

create trigger etsy_orders_set_updated_at before update on app.etsy_orders
  for each row execute function private.set_updated_at();
create trigger etsy_order_sync_state_set_updated_at before update on app.etsy_order_sync_state
  for each row execute function private.set_updated_at();

alter table app.etsy_orders enable row level security;
alter table app.etsy_order_items enable row level security;
alter table app.etsy_order_refunds enable row level security;
alter table app.etsy_order_buyers enable row level security;
alter table app.etsy_order_sync_state enable row level security;
revoke all on app.etsy_orders, app.etsy_order_items, app.etsy_order_refunds,
  app.etsy_order_buyers, app.etsy_order_sync_state
  from public, anon, authenticated, service_role;

grant select on app.etsy_orders, app.etsy_order_items, app.etsy_order_refunds,
  app.etsy_order_buyers, app.etsy_order_sync_state to authenticated;

create policy etsy_orders_select on app.etsy_orders for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy etsy_order_items_select on app.etsy_order_items for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy etsy_order_refunds_select on app.etsy_order_refunds for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy etsy_order_sync_state_select on app.etsy_order_sync_state for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy etsy_order_buyers_select on app.etsy_order_buyers for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_buyer_data_shop_ids()))
  );

-- The purge: every stored order (items, refunds and buyer rows cascade) and the sync state of
-- shops disconnected past their `purge_after`. Returns the number of orders deleted. Only the
-- API's login role (and the Phase 5 worker) may run it.
create function private.purge_disconnected_etsy_orders(p_now timestamptz) returns integer
language plpgsql security definer set search_path = '' as $$
declare
  purged integer;
begin
  delete from app.etsy_order_sync_state s
  using app.shop_integrations i
  where i.shop_id = s.shop_id and i.status = 'disconnected' and i.purge_after <= p_now;
  with gone as (
    delete from app.etsy_orders o
    using app.shop_integrations i
    where i.shop_id = o.shop_id and i.status = 'disconnected' and i.purge_after <= p_now
    returning 1
  )
  select count(*) into purged from gone;
  return purged;
end
$$;
revoke all on function private.purge_disconnected_etsy_orders(timestamptz)
  from public, anon, authenticated, service_role;
