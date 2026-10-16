-- Capability probes (slice 4.5) and disconnect (slice 4.7), ADR-0008.
--
-- `app.shop_capabilities` keeps the latest probe result per shop and capability: a state
-- (available only after a successful probe, AC-4.5.1), a short machine code saying why, and when
-- it was checked. Probes only read from Etsy and store presence, never values: no column here can
-- hold buyer data (the detail is a short lower-case code). Only the API's login role writes; an
-- Owner or Admin reads their organization's rows, like `app.shop_integrations`.
--
-- A disconnect keeps the integration row without tokens and records when, and after which date
-- the shop's data may be purged (30 days; the purge job runs from Phase 5). A reconnect clears
-- both. Additive only: existing rows keep working (both columns start null).

create table app.shop_capabilities (
  id uuid primary key,
  organization_id uuid not null,
  shop_id uuid not null,
  capability text not null constraint shop_capabilities_capability_check check (
    capability in (
      'granted_scopes', 'shop_details', 'receipts_read', 'buyer_address', 'listings_read',
      'webhooks', 'shipment_write'
    )
  ),
  state text not null constraint shop_capabilities_state_check check (
    state in ('available', 'unavailable', 'unknown')
  ),
  detail text not null constraint shop_capabilities_detail_check check (detail ~ '^[a-z0-9_]{1,64}$'),
  checked_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint shop_capabilities_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint shop_capabilities_shop_id_capability_key unique (shop_id, capability)
);
create index shop_capabilities_organization_id_idx on app.shop_capabilities (organization_id);

create trigger shop_capabilities_set_updated_at before update on app.shop_capabilities
  for each row execute function private.set_updated_at();

alter table app.shop_capabilities enable row level security;
revoke all on app.shop_capabilities from public, anon, authenticated, service_role;

grant select (
  id, organization_id, shop_id, capability, state, detail, checked_at, created_at, updated_at
) on app.shop_capabilities to authenticated;

create policy shop_capabilities_select on app.shop_capabilities for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));

-- Re-checks are claimed here before probing, so two presses at once cannot both call Etsy (at most
-- one re-check per shop every 30 seconds). Not granted to any client role.
alter table app.shop_integrations add column capability_check_reserved_at timestamptz;

-- Disconnect (slice 4.7).
alter table app.shop_integrations
  add column disconnected_at timestamptz,
  add column purge_after timestamptz,
  add constraint shop_integrations_disconnect_dates_check check (
    case when status = 'disconnected' then true
    else disconnected_at is null and purge_after is null
    end
  ),
  add constraint shop_integrations_purge_after_check check (
    purge_after is null or (disconnected_at is not null and purge_after > disconnected_at)
  );

grant select (disconnected_at, purge_after) on app.shop_integrations to authenticated;

-- The purge job (Phase 5) finds disconnected shops past their date.
create index shop_integrations_purge_after_idx
  on app.shop_integrations (purge_after) where purge_after is not null;
