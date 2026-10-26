-- Slice 7.13: the supplier and quote model (moved from 13.1; docs/architecture/autova-integration.md).
--
-- suppliers         the organization's suppliers, with the name AutoVA shows for each.
-- quote_requests    one product (a listing, or one variant of it) waiting for a supplier, along
--                   htco_domain.suppliers.QUOTE_REQUEST_TRANSITIONS, checked again here; at most
--                   one open request per product.
-- supplier_quotes   money with its currency (never a float; AC-7.13.2), the shipping method and
--                   times, the validity and the source mail's reference. Never changed once stored.
-- supplier_mappings which supplier serves a product, from when and from which quote; at most one
--                   active mapping per product, and an ended one carries its reason (AC-7.13.1).
--
-- Identifiers, money and terms only: no buyer data, no mail content, no model output. Members read
-- their organization's rows (shop-scoped roles their shops' products); the worker (the quote parser
-- of 7.15 and the automation of 7.17) writes in its job's organization only. The Owner's choice
-- goes through an approval (7.17), so no member writes here directly.

create table app.suppliers (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  name text not null constraint suppliers_name_check check (char_length(btrim(name)) between 1 and 200),
  autova_name text
    constraint suppliers_autova_name_check check (char_length(btrim(autova_name)) between 1 and 200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint suppliers_id_organization_key unique (id, organization_id),
  constraint suppliers_name_key unique (organization_id, name)
);
create trigger suppliers_set_updated_at before update on app.suppliers
  for each row execute function private.set_updated_at();

create table app.quote_requests (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  listing_id bigint not null constraint quote_requests_listing_id_check check (listing_id > 0),
  -- Empty: the whole listing; otherwise one variant (for example an Etsy product ID).
  variant text not null default ''
    constraint quote_requests_variant_check check (char_length(variant) <= 120),
  state text not null constraint quote_requests_state_check check (
    state in ('waiting_for_quotes', 'quotes_received', 'chosen', 'confirmed_in_autova', 'mapped',
              'cancelled')
  ),
  chosen_quote_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null,
  constraint quote_requests_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint quote_requests_id_organization_key unique (id, organization_id),
  -- A choice names its quote from CHOSEN on; going back to the quotes clears it.
  constraint quote_requests_chosen_check check (
    (state in ('chosen', 'confirmed_in_autova', 'mapped')) = (chosen_quote_id is not null)
  )
);
create unique index quote_requests_open_product on app.quote_requests
  (organization_id, shop_id, listing_id, variant) where state not in ('mapped', 'cancelled');
create index quote_requests_organization_state_idx on app.quote_requests (organization_id, state);

create table app.supplier_quotes (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  quote_request_id uuid not null,
  supplier_id uuid not null,
  currency text not null constraint supplier_quotes_currency_check check (currency ~ '^[A-Z]{3}$'),
  unit_price numeric(14, 4) not null constraint supplier_quotes_unit_price_check check (unit_price > 0),
  shipping_cost numeric(14, 4) not null
    constraint supplier_quotes_shipping_cost_check check (shipping_cost >= 0),
  shipping_method text not null
    constraint supplier_quotes_shipping_method_check check (char_length(btrim(shipping_method)) between 1 and 120),
  processing_days_min smallint not null,
  processing_days_max smallint not null,
  shipping_days_min smallint not null,
  shipping_days_max smallint not null,
  valid_until date,
  -- The quote mail's reference (a hash of its message ID), never its content.
  source_reference text not null
    constraint supplier_quotes_source_reference_check check (char_length(source_reference) between 1 and 200),
  received_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint supplier_quotes_request_fkey foreign key (quote_request_id, organization_id)
    references app.quote_requests (id, organization_id) on delete cascade,
  constraint supplier_quotes_supplier_fkey foreign key (supplier_id, organization_id)
    references app.suppliers (id, organization_id) on delete restrict,
  constraint supplier_quotes_id_organization_key unique (id, organization_id),
  constraint supplier_quotes_processing_days_check check (
    processing_days_min between 0 and 365 and processing_days_max between processing_days_min and 365
  ),
  constraint supplier_quotes_shipping_days_check check (
    shipping_days_min between 0 and 365 and shipping_days_max between shipping_days_min and 365
  ),
  -- The same mail parsed twice stores one quote.
  constraint supplier_quotes_source_key unique (quote_request_id, source_reference)
);
create index supplier_quotes_request_idx on app.supplier_quotes (quote_request_id);
create index supplier_quotes_organization_id_idx on app.supplier_quotes (organization_id);

alter table app.quote_requests add constraint quote_requests_chosen_quote_fkey
  foreign key (chosen_quote_id, organization_id)
  references app.supplier_quotes (id, organization_id) on delete restrict;

create table app.supplier_mappings (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  listing_id bigint not null constraint supplier_mappings_listing_id_check check (listing_id > 0),
  variant text not null default ''
    constraint supplier_mappings_variant_check check (char_length(variant) <= 120),
  supplier_id uuid not null,
  quote_id uuid,
  active_from timestamptz not null,
  ended_at timestamptz,
  end_reason text constraint supplier_mappings_end_reason_check check (
    end_reason in ('replaced', 'supplier_stopped', 'product_retired')
  ),
  created_at timestamptz not null default now(),
  constraint supplier_mappings_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint supplier_mappings_supplier_fkey foreign key (supplier_id, organization_id)
    references app.suppliers (id, organization_id) on delete restrict,
  constraint supplier_mappings_quote_fkey foreign key (quote_id, organization_id)
    references app.supplier_quotes (id, organization_id) on delete restrict,
  constraint supplier_mappings_end_check check (
    (ended_at is null) = (end_reason is null) and (ended_at is null or ended_at >= active_from)
  )
);
-- AC-7.13.1: at most one active mapping per product.
create unique index supplier_mappings_active_product on app.supplier_mappings
  (organization_id, shop_id, listing_id, variant) where ended_at is null;
create index supplier_mappings_organization_id_idx on app.supplier_mappings (organization_id);

-- The quote request's machine (htco_domain.suppliers.QUOTE_REQUEST_TRANSITIONS), checked again.
create function private.quote_request_edge_allowed(from_state text, to_state text) returns boolean
language sql immutable set search_path = '' as $$
  select (from_state, to_state) in (
    ('waiting_for_quotes', 'quotes_received'), ('waiting_for_quotes', 'cancelled'),
    ('quotes_received', 'chosen'), ('quotes_received', 'cancelled'),
    ('chosen', 'confirmed_in_autova'), ('chosen', 'quotes_received'), ('chosen', 'cancelled'),
    ('confirmed_in_autova', 'mapped'), ('confirmed_in_autova', 'cancelled')
  )
$$;
revoke all on function private.quote_request_edge_allowed(text, text) from public;

create function private.check_quote_request_state() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    if new.state <> 'waiting_for_quotes' and new.state <> 'quotes_received' then
      raise exception 'quote request: a request starts waiting for quotes, not %', new.state
        using errcode = 'check_violation';
    end if;
    return new;
  end if;
  if new.state <> old.state and not private.quote_request_edge_allowed(old.state, new.state) then
    raise exception 'quote request: % -> % is not allowed', old.state, new.state
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_quote_request_state() from public;
create trigger quote_requests_check_state before insert or update of state on app.quote_requests
  for each row execute function private.check_quote_request_state();

-- An ended mapping stays ended, with the same reason and the same product and supplier.
create function private.check_supplier_mapping_change() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if old.ended_at is not null
     or (new.organization_id, new.shop_id, new.listing_id, new.variant, new.supplier_id,
         new.quote_id, new.active_from)
        is distinct from
        (old.organization_id, old.shop_id, old.listing_id, old.variant, old.supplier_id,
         old.quote_id, old.active_from) then
    raise exception 'supplier mapping: only an active mapping can end, and nothing else changes'
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_supplier_mapping_change() from public;
create trigger supplier_mappings_check_change before update on app.supplier_mappings
  for each row execute function private.check_supplier_mapping_change();

-- Row-level security ------------------------------------------------------------------------------

alter table app.suppliers enable row level security;
alter table app.quote_requests enable row level security;
alter table app.supplier_quotes enable row level security;
alter table app.supplier_mappings enable row level security;
revoke all on app.suppliers, app.quote_requests, app.supplier_quotes, app.supplier_mappings
  from public, anon, authenticated, service_role;

-- Members: the organization's suppliers; quote requests, quotes and mappings of their shops.
grant select on app.suppliers, app.quote_requests, app.supplier_quotes, app.supplier_mappings
  to authenticated;
create policy suppliers_select on app.suppliers for select to authenticated
  using (organization_id = any (array(select private.user_org_ids())));
create policy quote_requests_select on app.quote_requests for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy supplier_quotes_select on app.supplier_quotes for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and exists (
      select 1 from app.quote_requests r
      where r.id = quote_request_id
        and r.shop_id = any (array(select private.user_shop_ids()))
    )
  );
create policy supplier_mappings_select on app.supplier_mappings for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );

-- The worker: its job's organization only. Quotes are never changed; a mapping only ends.
grant select, insert on app.suppliers to htco_worker;
grant update (autova_name, updated_at) on app.suppliers to htco_worker;
create policy suppliers_worker on app.suppliers for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

grant select, insert on app.quote_requests to htco_worker;
grant update (state, chosen_quote_id, updated_at) on app.quote_requests to htco_worker;
create policy quote_requests_worker on app.quote_requests for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

grant select, insert on app.supplier_quotes to htco_worker;
create policy supplier_quotes_worker on app.supplier_quotes for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

grant select, insert on app.supplier_mappings to htco_worker;
grant update (ended_at, end_reason) on app.supplier_mappings to htco_worker;
create policy supplier_mappings_worker on app.supplier_mappings for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
