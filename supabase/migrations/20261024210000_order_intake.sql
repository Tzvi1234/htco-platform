-- Etsy Order Intake on the worker (slice 5.4): receipts ingested through the engine into the #57
-- order history store, with the order state machine, its transitions and basic financial entries.
--
-- `app.etsy_orders` stays the order store (D-19): no second orders table. It gains the order's
-- state (FR-ORD-03, derived from Etsy's facts) and the SHA-256 of the receipt's order facts (no
-- buyer data in the hash input), so a replay is recognised and changes nothing.
--
-- `app.order_status_events`: every transition with its source (`webhook`, `reconciliation`,
-- `backfill`, `sync`), when it was observed and Etsy's update time as proof. A trigger refuses any
-- pair outside the machine (the domain refuses them first).
--
-- `app.financial_entries`: basic ledger lines, labelled `estimated` (revenue: the receipt's grand
-- total; each refund as a negative line). Etsy fees need the payment ledger (Phase 13).
--
-- The worker (`htco_worker`) now writes the store inside its job's organization, as the API's login
-- role does. Least privilege: it reads only the columns the ingest compares (state, times, hash,
-- status flags, item quantities); it can write the buyer table but never read a name or an
-- address (no select on those columns), and it reads no money column.
--
-- Safe on the live database: two nullable columns (no rewrite), two new tables, grants, policies.

alter table app.etsy_orders
  add column order_state text constraint etsy_orders_order_state_check check (
    order_state in ('unpaid', 'paid', 'ingested', 'awaiting_shipment', 'shipped', 'delivered',
                    'cancelled', 'refunded')
  ),
  add column receipt_sha256 bytea
    constraint etsy_orders_receipt_sha256_check check (octet_length(receipt_sha256) = 32);

create table app.order_status_events (
  id uuid primary key,
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  from_state text constraint order_status_events_from_state_check check (
    from_state in ('unpaid', 'paid', 'ingested', 'awaiting_shipment', 'shipped', 'delivered',
                   'cancelled', 'refunded')
  ),
  to_state text not null constraint order_status_events_to_state_check check (
    to_state in ('unpaid', 'paid', 'ingested', 'awaiting_shipment', 'shipped', 'delivered',
                 'cancelled', 'refunded')
  ),
  source text not null constraint order_status_events_source_check check (
    source in ('webhook', 'reconciliation', 'backfill', 'sync')
  ),
  observed_at timestamptz not null,
  -- Proof: Etsy's update time of the receipt that showed the state, and its facts' hash.
  etsy_updated_at timestamptz not null,
  receipt_sha256 bytea not null
    constraint order_status_events_receipt_sha256_check check (octet_length(receipt_sha256) = 32),
  execution_id uuid,
  correlation_id text
    constraint order_status_events_correlation_id_check check (char_length(correlation_id) <= 200),
  created_at timestamptz not null default now(),
  constraint order_status_events_order_fkey foreign key (shop_id, receipt_id)
    references app.etsy_orders (shop_id, receipt_id) on delete cascade
);
create index order_status_events_order_idx
  on app.order_status_events (shop_id, receipt_id, observed_at);
create index order_status_events_organization_id_idx on app.order_status_events (organization_id);

-- The order machine (htco_domain.orders.ORDER_TRANSITIONS), checked again by the database.
create function private.check_order_transition() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.from_state is null then
    if new.to_state not in ('unpaid', 'paid') then
      raise exception 'order: an order starts unpaid or paid, not %', new.to_state
        using errcode = 'check_violation';
    end if;
    return new;
  end if;
  if not (
    (new.from_state = 'unpaid' and new.to_state in ('paid', 'cancelled'))
    or (new.from_state = 'paid' and new.to_state = 'ingested')
    or (new.from_state = 'ingested'
        and new.to_state in ('awaiting_shipment', 'shipped', 'cancelled', 'refunded'))
    or (new.from_state = 'awaiting_shipment' and new.to_state in ('shipped', 'cancelled', 'refunded'))
    or (new.from_state = 'shipped' and new.to_state in ('delivered', 'refunded'))
    or (new.from_state = 'delivered' and new.to_state = 'refunded')
  ) then
    raise exception 'order: % -> % is not allowed', new.from_state, new.to_state
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_order_transition() from public;
create trigger order_status_events_check_transition before insert on app.order_status_events
  for each row execute function private.check_order_transition();

create table app.financial_entries (
  id uuid primary key,
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  kind text not null constraint financial_entries_kind_check check (kind in ('revenue', 'refund')),
  -- The line's place among its kind on the receipt (refunds oldest first; revenue is 0).
  position integer not null constraint financial_entries_position_check check (position >= 0),
  -- Exact: a Decimal with its currency, never a float.
  amount numeric(20, 6) not null,
  currency text not null constraint financial_entries_currency_check check (currency ~ '^[A-Z]{3}$'),
  certainty text not null default 'estimated'
    constraint financial_entries_certainty_check check (certainty in ('estimated', 'verified')),
  source text not null constraint financial_entries_source_check check (source = 'etsy_receipt'),
  occurred_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint financial_entries_order_fkey foreign key (shop_id, receipt_id)
    references app.etsy_orders (shop_id, receipt_id) on delete cascade,
  constraint financial_entries_line_key unique (shop_id, receipt_id, kind, position)
);
create index financial_entries_organization_id_idx on app.financial_entries (organization_id);
create trigger financial_entries_set_updated_at before update on app.financial_entries
  for each row execute function private.set_updated_at();

alter table app.order_status_events enable row level security;
alter table app.financial_entries enable row level security;
revoke all on app.order_status_events, app.financial_entries
  from public, anon, authenticated, service_role;

-- Members read their shops' order history and its estimated money, like the orders themselves.
grant select on app.order_status_events, app.financial_entries to authenticated;
create policy order_status_events_select on app.order_status_events for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy financial_entries_select on app.financial_entries for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );

-- The worker: its job's organization only -----------------------------------------------------

-- Orders: the columns the ingest compares, never a money column; insert and update the rows.
grant select (organization_id, shop_id, receipt_id, etsy_shop_id, status, is_paid, is_shipped,
              shipments, etsy_updated_at, order_state, receipt_sha256)
  on app.etsy_orders to htco_worker;
grant insert, update on app.etsy_orders to htco_worker;
create policy etsy_orders_worker_insert on app.etsy_orders for insert to htco_worker
  with check (organization_id = (select private.worker_organization_id()));
create policy etsy_orders_worker_update on app.etsy_orders for update to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- Items: replaced as a whole with the receipt; the quantities are compared by reconciliation.
grant select (organization_id, shop_id, receipt_id, transaction_id, position, listing_id, quantity)
  on app.etsy_order_items to htco_worker;
grant insert, delete on app.etsy_order_items to htco_worker;
-- Refunds and the buyer row: written and replaced, never read beyond their keys.
grant select (organization_id, shop_id, receipt_id) on app.etsy_order_refunds to htco_worker;
grant insert, delete on app.etsy_order_refunds to htco_worker;
grant select (organization_id, shop_id, receipt_id) on app.etsy_order_buyers to htco_worker;
grant insert, delete on app.etsy_order_buyers to htco_worker;
create policy etsy_order_items_worker on app.etsy_order_items for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
create policy etsy_order_refunds_worker on app.etsy_order_refunds for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
create policy etsy_order_buyers_worker on app.etsy_order_buyers for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- The sync state (no personal data): the checkpoint and the times.
grant select, insert, update on app.etsy_order_sync_state to htco_worker;
create policy etsy_order_sync_state_worker_insert on app.etsy_order_sync_state for insert
  to htco_worker with check (organization_id = (select private.worker_organization_id()));
create policy etsy_order_sync_state_worker_update on app.etsy_order_sync_state for update
  to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
-- The sync state's own select policy for the worker exists (20261023190000); full columns now.

grant select, insert on app.order_status_events to htco_worker;
create policy order_status_events_worker on app.order_status_events for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
-- Replaced as a whole with the receipt, like its items.
grant select (organization_id, shop_id, receipt_id, kind, position), insert, delete
  on app.financial_entries to htco_worker;
create policy financial_entries_worker on app.financial_entries for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
