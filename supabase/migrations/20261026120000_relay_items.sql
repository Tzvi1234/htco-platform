-- The AutoVA tracking relay (slice 6.10): one relay item per paid order whose tracking has not
-- reached Etsy within the relay window (2 business days after payment, ASSUMPTIONS A-20).
--
-- `app.relay_items`: owned by Phase 6 (audit P1-4); Phase 7's task store extends it later. One
-- item per order, ever (`shop_id, receipt_id` unique, AC-6.10.1). It keeps the state, why it
-- closed, and the tracking the owner pasted as carrier, fingerprint and last four characters:
-- the same fingerprint as `tracking_events`, so a number is never used twice (AC-6.10.2).
-- `app.relay_item_codes`: the full pasted code, buyer data (ADR-0018). It exists only while the
-- item waits for its upload, is read only by the buyer-data roles of its shop, and is deleted
-- when the item closes.
--
-- The API writes both through its own role; the worker opens and closes items in its job's
-- organization and may delete a closed item's code. No Etsy write is involved (6.8 adds it).
--
-- Safe on the live database: new tables, grants and policies.

create table app.relay_items (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  receipt_id bigint not null constraint relay_items_receipt_id_check check (receipt_id > 0),
  state text not null constraint relay_items_state_check check (
    state in ('awaiting_tracking', 'handed_off', 'closed')
  ),
  closed_reason text constraint relay_items_closed_reason_check check (
    closed_reason in ('tracking_on_etsy', 'uploaded', 'cancelled')
  ),
  carrier text constraint relay_items_carrier_check check (carrier ~ '^[a-z0-9_]{2,20}$'),
  tracking_fingerprint bytea
    constraint relay_items_fingerprint_check check (octet_length(tracking_fingerprint) = 32),
  tracking_last4 text constraint relay_items_last4_check check (tracking_last4 ~ '^[A-Z0-9]{1,4}$'),
  paid_at timestamptz not null,
  opened_at timestamptz not null,
  updated_at timestamptz not null,
  closed_at timestamptz,
  constraint relay_items_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint relay_items_order_key unique (shop_id, receipt_id),
  -- A closed item says why; an open one does not.
  constraint relay_items_closed_check check (
    (state = 'closed') = (closed_reason is not null and closed_at is not null)
  ),
  -- Handed off only with the pasted tracking.
  constraint relay_items_tracking_check check (
    state <> 'handed_off' or (carrier is not null and tracking_fingerprint is not null)
  )
);
create index relay_items_open_idx on app.relay_items (organization_id, opened_at desc)
  where state <> 'closed';
-- A number is pasted for one order of a shop only.
create unique index relay_items_fingerprint_key on app.relay_items (shop_id, tracking_fingerprint)
  where tracking_fingerprint is not null;

create table app.relay_item_codes (
  relay_item_id uuid primary key references app.relay_items (id) on delete cascade,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  code text not null constraint relay_item_codes_code_check check (code ~ '^[A-Z0-9]{8,40}$'),
  constraint relay_item_codes_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
);

alter table app.relay_items enable row level security;
alter table app.relay_item_codes enable row level security;
revoke all on app.relay_items, app.relay_item_codes from public, anon, authenticated, service_role;

-- Members: their shops' relay items; the code only for the shop's buyer-data roles.
grant select on app.relay_items, app.relay_item_codes to authenticated;
create policy relay_items_select on app.relay_items for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy relay_item_codes_select on app.relay_item_codes for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_buyer_data_shop_ids()))
  );

-- The worker: its job's organization only. It opens items, closes them on evidence, and deletes
-- a closed item's code; it never reads a code.
grant select, insert on app.relay_items to htco_worker;
grant update (state, closed_reason, updated_at, closed_at) on app.relay_items to htco_worker;
create policy relay_items_worker on app.relay_items for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
grant delete on app.relay_item_codes to htco_worker;
grant select (relay_item_id, organization_id) on app.relay_item_codes to htco_worker;
create policy relay_item_codes_worker on app.relay_item_codes for delete to htco_worker
  using (organization_id = (select private.worker_organization_id()));
