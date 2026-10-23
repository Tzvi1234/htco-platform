-- Reconciliation on the queue (slice 5.5): drifts between the order store and Etsy, and the
-- reconciliation cursors per shop.
--
-- `app.sync_drifts`: one row per difference reconciliation found (an order HTCO never stored,
-- its status, its items or its shipments), with both values. The values carry order facts only:
-- statuses, item IDs and quantities, shipment carriers and times; never money, a buyer's name, an
-- address or a tracking number. Unique per receipt, kind and Etsy update time, so a repeated
-- reconciliation records a difference once. From Phase 6 a drift opens an incident (FR-ORD-09).
--
-- `app.sync_cursors`: per shop, resource and kind (`reconcile`, `sweep`), when a run last started,
-- when one last succeeded (the shop's freshness) and the last error type.
--
-- The worker writes both inside its job's organization; members read their shops' rows.
--
-- Safe on the live database: two new tables, grants and policies only.

create table app.sync_drifts (
  id uuid primary key,
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  kind text not null constraint sync_drifts_kind_check check (
    kind in ('missing', 'status', 'items', 'shipments')
  ),
  stored jsonb,
  observed jsonb not null,
  etsy_updated_at timestamptz not null,
  source text not null constraint sync_drifts_source_check check (
    source in ('reconciliation', 'webhook', 'backfill', 'sync')
  ),
  detected_at timestamptz not null,
  execution_id uuid,
  correlation_id text
    constraint sync_drifts_correlation_id_check check (char_length(correlation_id) <= 200),
  created_at timestamptz not null default now(),
  constraint sync_drifts_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint sync_drifts_values_size_check check (
    pg_column_size(stored) <= 8192 and pg_column_size(observed) <= 8192
  ),
  constraint sync_drifts_receipt_kind_key unique (shop_id, receipt_id, kind, etsy_updated_at)
);
create index sync_drifts_organization_id_idx on app.sync_drifts (organization_id);
create index sync_drifts_shop_detected_idx on app.sync_drifts (shop_id, detected_at desc);

create table app.sync_cursors (
  organization_id uuid not null,
  shop_id uuid not null,
  resource text not null constraint sync_cursors_resource_check check (resource = 'orders'),
  kind text not null constraint sync_cursors_kind_check check (kind in ('reconcile', 'sweep')),
  last_started_at timestamptz,
  last_succeeded_at timestamptz,
  -- An exception type name, never a message (messages can quote buyer data).
  last_error text constraint sync_cursors_last_error_check check (
    last_error ~ '^[A-Za-z0-9_.: -]{1,64}$'
  ),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (shop_id, resource, kind),
  constraint sync_cursors_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
);
create index sync_cursors_organization_id_idx on app.sync_cursors (organization_id);
create trigger sync_cursors_set_updated_at before update on app.sync_cursors
  for each row execute function private.set_updated_at();

alter table app.sync_drifts enable row level security;
alter table app.sync_cursors enable row level security;
revoke all on app.sync_drifts, app.sync_cursors from public, anon, authenticated, service_role;

grant select on app.sync_drifts, app.sync_cursors to authenticated;
create policy sync_drifts_select on app.sync_drifts for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy sync_cursors_select on app.sync_cursors for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );

grant select, insert on app.sync_drifts to htco_worker;
create policy sync_drifts_worker on app.sync_drifts for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
grant select, insert, update on app.sync_cursors to htco_worker;
create policy sync_cursors_worker on app.sync_cursors for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
