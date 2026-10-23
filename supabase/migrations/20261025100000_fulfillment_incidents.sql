-- Fulfillment monitoring and incidents (slices 6.1 to 6.3).
--
-- `app.tracking_events`: one row per shipment and order, the first time HTCO saw its tracking
-- (AC-6.1.1, AC-6.1.2): the carrier, the code's SHA-256 fingerprint and last four characters,
-- where it came from (`etsy`, or the AutoVA relay's sources of slice 6.10) and which trigger saw
-- it. The full code is buyer data: it stays in `app.etsy_order_buyers.shipments`, which only
-- buyer-data roles read.
--
-- `app.sla_policies`: the window from payment to the first tracking per organization, shop,
-- supplier and shipping method, in calendar or business days with the calendar's weekend and
-- holidays (slice 6.2). No row is the built-in generous default (10 business days).
--
-- `app.incidents` and `app.incident_events` (slice 6.3, FR-INC-02, FR-INC-03, FR-INC-05): one
-- open incident per deduplication key (a partial unique index), the machine checked again here,
-- `resolved` only with evidence, and the events append-only.
--
-- The worker (`htco_worker`) reads and writes these in its job's organization only. It gains read
-- access to `etsy_orders.etsy_created_at` (the payment time the SLA counts from); no money and no
-- buyer column.
--
-- Safe on the live database: new tables, one column grant, policies.

create table app.tracking_events (
  id uuid primary key,
  organization_id uuid not null,
  shop_id uuid not null,
  receipt_id bigint not null,
  carrier text not null constraint tracking_events_carrier_check check (carrier ~ '^[a-z0-9_]{1,32}$'),
  carrier_name text not null
    constraint tracking_events_carrier_name_check check (char_length(carrier_name) between 1 and 64),
  fingerprint bytea not null
    constraint tracking_events_fingerprint_check check (octet_length(fingerprint) = 32),
  last4 text not null constraint tracking_events_last4_check check (last4 ~ '^[A-Z0-9]{1,4}$'),
  source text not null constraint tracking_events_source_check check (
    source in ('etsy', 'autova_manual', 'autova_browser', 'autova_email')
  ),
  observed_via text not null constraint tracking_events_observed_via_check check (
    observed_via in ('webhook', 'schedule', 'relay')
  ),
  first_observed_at timestamptz not null,
  notified_at timestamptz,
  execution_id uuid,
  correlation_id text
    constraint tracking_events_correlation_id_check check (char_length(correlation_id) <= 200),
  created_at timestamptz not null default now(),
  constraint tracking_events_order_fkey foreign key (shop_id, receipt_id)
    references app.etsy_orders (shop_id, receipt_id) on delete cascade,
  constraint tracking_events_parcel_key unique (shop_id, receipt_id, fingerprint)
);
create index tracking_events_organization_id_idx on app.tracking_events (organization_id);

create table app.sla_policies (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid,
  supplier text constraint sla_policies_supplier_check check (char_length(supplier) between 1 and 120),
  shipping_method text
    constraint sla_policies_shipping_method_check check (char_length(shipping_method) between 1 and 120),
  window_days integer not null constraint sla_policies_window_days_check check (window_days between 1 and 90),
  unit text not null constraint sla_policies_unit_check check (unit in ('calendar_days', 'business_days')),
  timezone text not null default 'UTC'
    constraint sla_policies_timezone_check check (timezone ~ '^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$'),
  weekend smallint[] not null default '{5,6}'
    constraint sla_policies_weekend_check check (
      weekend <@ '{0,1,2,3,4,5,6}'::smallint[] and cardinality(weekend) <= 6
    ),
  holidays date[] not null default '{}'
    constraint sla_policies_holidays_check check (cardinality(holidays) <= 400),
  version integer not null default 1 constraint sla_policies_version_check check (version >= 1),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint sla_policies_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint sla_policies_scope_key unique nulls not distinct
    (organization_id, shop_id, supplier, shipping_method)
);
create trigger sla_policies_set_updated_at before update on app.sla_policies
  for each row execute function private.set_updated_at();

create table app.incidents (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid,
  receipt_id bigint constraint incidents_receipt_id_check check (receipt_id > 0),
  dedup_key text not null
    constraint incidents_dedup_key_check check (dedup_key ~ '^[a-z_]{1,40}:[a-z_]{1,20}:[^:]{1,120}$'),
  cause text not null constraint incidents_cause_check check (
    cause in ('tracking_missing', 'sync_drift', 'tracking_mismatch', 'automation_dead_lettered')
  ),
  resource_kind text not null
    constraint incidents_resource_kind_check check (resource_kind in ('order', 'automation')),
  resource_id text not null
    constraint incidents_resource_id_check check (char_length(resource_id) between 1 and 120),
  severity text not null
    constraint incidents_severity_check check (severity in ('low', 'medium', 'high', 'critical')),
  state text not null constraint incidents_state_check check (
    state in ('detected', 'triaged', 'investigating', 'remediation_proposed', 'auto_remediating',
              'approval_required', 'developer_required', 'verifying', 'resolved', 'escalated')
  ),
  source text not null constraint incidents_source_check check (source ~ '^[a-z_]{1,40}$'),
  -- Identifiers and figures of the detection; never buyer data.
  details jsonb not null default '{}'::jsonb constraint incidents_details_check check (
    jsonb_typeof(details) = 'object' and pg_column_size(details) <= 4096
  ),
  detected_at timestamptz not null,
  updated_at timestamptz not null,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  constraint incidents_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint incidents_resolved_check check ((state = 'resolved') = (resolved_at is not null))
);
-- FR-INC-03: exactly one open incident per root cause and resource.
create unique index incidents_open_dedup_key on app.incidents (organization_id, dedup_key)
  where state <> 'resolved';
create index incidents_organization_state_idx on app.incidents (organization_id, state, detected_at desc);
create index incidents_shop_receipt_idx on app.incidents (shop_id, receipt_id);

-- The incident machine (htco_domain.incidents.INCIDENT_TRANSITIONS), checked again here.
create function private.incident_edge_allowed(from_state text, to_state text) returns boolean
language sql immutable set search_path = '' as $$
  select (from_state, to_state) in (
    ('detected', 'triaged'), ('detected', 'verifying'), ('detected', 'escalated'),
    ('triaged', 'investigating'), ('triaged', 'verifying'), ('triaged', 'escalated'),
    ('investigating', 'remediation_proposed'), ('investigating', 'verifying'),
    ('investigating', 'escalated'),
    ('remediation_proposed', 'auto_remediating'), ('remediation_proposed', 'approval_required'),
    ('remediation_proposed', 'developer_required'), ('remediation_proposed', 'escalated'),
    ('auto_remediating', 'verifying'), ('auto_remediating', 'escalated'),
    ('approval_required', 'verifying'), ('approval_required', 'escalated'),
    ('developer_required', 'verifying'), ('developer_required', 'escalated'),
    ('verifying', 'resolved'), ('verifying', 'investigating'), ('verifying', 'escalated'),
    ('escalated', 'triaged'), ('escalated', 'investigating'), ('escalated', 'verifying')
  )
$$;
revoke all on function private.incident_edge_allowed(text, text) from public;

create function private.check_incident_state() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.state = old.state then
    return new;
  end if;
  -- One stored update may cover the recorded path (for example detected -> verifying -> resolved):
  -- the events carry each edge; the row only needs a path that exists.
  if not (
    private.incident_edge_allowed(old.state, new.state)
    or (new.state = 'resolved' and old.state <> 'resolved')
  ) then
    raise exception 'incident: % -> % is not allowed', old.state, new.state
      using errcode = 'check_violation';
  end if;
  if new.state = 'resolved' and not exists (
    select 1 from app.incident_events e
    where e.incident_id = new.id and e.to_state = 'resolved' and e.evidence_kind is not null
  ) then
    raise exception 'incident: resolved only with evidence' using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_incident_state() from public;
create trigger incidents_check_state before update of state on app.incidents
  for each row execute function private.check_incident_state();

create table app.incident_events (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  incident_id uuid not null references app.incidents (id) on delete cascade,
  from_state text,
  to_state text not null,
  actor_kind text not null constraint incident_events_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null constraint incident_events_actor_id_check check (char_length(actor_id) <= 200),
  occurred_at timestamptz not null,
  evidence_kind text constraint incident_events_evidence_kind_check check (
    evidence_kind in ('tracking_present', 'order_closed', 'drift_cleared')
  ),
  evidence_source text constraint incident_events_evidence_source_check check (
    char_length(evidence_source) between 1 and 64
  ),
  evidence_reference text constraint incident_events_evidence_reference_check check (
    char_length(evidence_reference) between 1 and 200
  ),
  evidence_observed_at timestamptz,
  note text constraint incident_events_note_check check (note ~ '^[a-z0-9_.:-]{1,64}$'),
  correlation_id text
    constraint incident_events_correlation_id_check check (char_length(correlation_id) <= 200),
  created_at timestamptz not null default now(),
  constraint incident_events_evidence_check check (
    (evidence_kind is null) = (evidence_source is null)
    and (evidence_kind is null) = (evidence_reference is null)
    and (evidence_kind is null) = (evidence_observed_at is null)
  ),
  constraint incident_events_resolution_check check (to_state <> 'resolved' or evidence_kind is not null)
);
create index incident_events_incident_idx on app.incident_events (incident_id, occurred_at);
create index incident_events_organization_id_idx on app.incident_events (organization_id);

create function private.check_incident_event() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.from_state is null then
    if new.to_state <> 'detected' then
      raise exception 'incident: an incident starts detected, not %', new.to_state
        using errcode = 'check_violation';
    end if;
  elsif not private.incident_edge_allowed(new.from_state, new.to_state) then
    raise exception 'incident: % -> % is not allowed', new.from_state, new.to_state
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_incident_event() from public;
create trigger incident_events_check_edge before insert on app.incident_events
  for each row execute function private.check_incident_event();

create function private.reject_incident_event_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'app.incident_events is append-only' using errcode = '42501';
end;
$$;
revoke all on function private.reject_incident_event_change() from public;
create trigger incident_events_append_only before update or delete on app.incident_events
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_incident_event_change();

alter table app.tracking_events enable row level security;
alter table app.sla_policies enable row level security;
alter table app.incidents enable row level security;
alter table app.incident_events enable row level security;
revoke all on app.tracking_events, app.sla_policies, app.incidents, app.incident_events
  from public, anon, authenticated, service_role;

-- Members: their shops' tracking events and incidents; organization-wide incidents (no shop) and
-- policies for every member of the organization.
grant select on app.tracking_events, app.sla_policies, app.incidents, app.incident_events
  to authenticated;
create policy tracking_events_select on app.tracking_events for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );
create policy sla_policies_select on app.sla_policies for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and (shop_id is null or shop_id = any (array(select private.user_shop_ids())))
  );
create policy incidents_select on app.incidents for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and (shop_id is null or shop_id = any (array(select private.user_shop_ids())))
  );
create policy incident_events_select on app.incident_events for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and exists (
      select 1 from app.incidents i
      where i.id = incident_id
        and (i.shop_id is null or i.shop_id = any (array(select private.user_shop_ids())))
    )
  );

-- The worker: its job's organization only --------------------------------------------------------

grant select (etsy_created_at) on app.etsy_orders to htco_worker;

grant select, insert on app.tracking_events to htco_worker;
create policy tracking_events_worker on app.tracking_events for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

grant select on app.sla_policies to htco_worker;
create policy sla_policies_worker on app.sla_policies for select to htco_worker
  using (organization_id = (select private.worker_organization_id()));

grant select, insert on app.incidents to htco_worker;
grant update (state, severity, updated_at, resolved_at) on app.incidents to htco_worker;
create policy incidents_worker on app.incidents for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

grant select, insert on app.incident_events to htco_worker;
create policy incident_events_worker on app.incident_events for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
