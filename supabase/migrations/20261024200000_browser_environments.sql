-- The Shop Environment Registry (slice 5.16, ADR-0020, execution-engine.md §7). Contracts only:
-- no browser runs before Phase 8A, and creating an environment is behind a flag that stays off.
--
-- `app.shop_browser_environments` holds at most one optional environment per shop, with the seven
-- statuses. The transition table of `htco_domain.browser` is enforced here again: a row is
-- created PROVISIONING (a shop without a row is NOT_CONFIGURED), and every status change outside
-- the table is refused. The record is honest by design: no fingerprint, user-agent or proxy
-- columns (ADR-0020).
--
-- `app.browser_environment_events` is the append-only history: every transition with its actor,
-- reason and correlation ID, and the health reports (problem codes only). An event's edge must
-- be one of the table's.
--
-- The API's login role writes (the registry); the Owner and Admins of the organization read
-- their environments and history; the worker reads its job's organization's environments (the
-- Execution Router's browser gate).

create function private.browser_environment_edge_allowed(from_status text, to_status text)
returns boolean
language sql immutable set search_path = '' as $$
  select (from_status, to_status) in (
    ('not_configured', 'provisioning'),
    ('provisioning', 'ready'), ('provisioning', 'login_required'), ('provisioning', 'failed'),
    ('ready', 'login_required'), ('ready', 'degraded'), ('ready', 'suspended'),
    ('login_required', 'ready'), ('login_required', 'suspended'),
    ('degraded', 'ready'), ('degraded', 'failed'), ('degraded', 'suspended'),
    ('suspended', 'ready'),
    ('failed', 'provisioning'),
    -- The Owner deletes the environment: profile, credentials and TOTP destroyed (audited).
    ('provisioning', 'not_configured'), ('ready', 'not_configured'),
    ('login_required', 'not_configured'), ('degraded', 'not_configured'),
    ('suspended', 'not_configured'), ('failed', 'not_configured')
  );
$$;

create table app.shop_browser_environments (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  status text not null constraint shop_browser_environments_status_check check (
    status in (
      'not_configured', 'provisioning', 'ready', 'login_required', 'degraded', 'suspended',
      'failed'
    )
  ),
  -- The account owner's consent (text version, user, time, SHA-256 of the address): all or none.
  consent_text_version text
    constraint shop_browser_environments_consent_text_version_check check (
      char_length(consent_text_version) between 1 and 64
    ),
  consent_user_id uuid,
  consent_at timestamptz,
  consent_ip_hash text
    constraint shop_browser_environments_consent_ip_hash_check check (
      consent_ip_hash ~ '^[0-9a-f]{64}$'
    ),
  -- The sealed profile archive's storage key and vault key ID (Phase 8A); never the profile.
  profile_storage_key text constraint shop_browser_environments_profile_storage_key_check check (
    char_length(profile_storage_key) <= 300
  ),
  profile_key_id text constraint shop_browser_environments_profile_key_id_check check (
    char_length(profile_key_id) <= 100
  ),
  challenge_count integer not null default 0
    constraint shop_browser_environments_challenge_count_check check (challenge_count >= 0),
  last_health_at timestamptz,
  last_health_ok boolean,
  last_session_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint shop_browser_environments_shop_id_key unique (shop_id),
  constraint shop_browser_environments_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint shop_browser_environments_consent_check check (
    num_nonnulls(consent_text_version, consent_user_id, consent_at, consent_ip_hash) in (0, 4)
  )
);
create index shop_browser_environments_organization_id_idx
  on app.shop_browser_environments (organization_id);

create trigger shop_browser_environments_set_updated_at
  before update on app.shop_browser_environments
  for each row execute function private.set_updated_at();

-- Created PROVISIONING only; every later status change follows the table.
create function private.check_browser_environment_status() returns trigger
language plpgsql set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    if new.status <> 'provisioning' then
      raise exception 'browser environment: created % instead of provisioning', new.status
        using errcode = '23514';
    end if;
    return new;
  end if;
  if new.status <> old.status
     and not private.browser_environment_edge_allowed(old.status, new.status) then
    raise exception 'browser environment: % -> % is not allowed', old.status, new.status
      using errcode = '23514';
  end if;
  return new;
end;
$$;
create trigger shop_browser_environments_check_status
  before insert or update of status on app.shop_browser_environments
  for each row execute function private.check_browser_environment_status();

create table app.browser_environment_events (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  environment_id uuid not null
    references app.shop_browser_environments (id) on delete cascade,
  kind text not null constraint browser_environment_events_kind_check check (
    kind in ('transition', 'health')
  ),
  from_status text,
  to_status text,
  health_ok boolean,
  -- Health problems are stable codes ("egress_ip_mismatch"), never free text.
  problems text[] not null default '{}'
    constraint browser_environment_events_problems_check check (
      cardinality(problems) <= 20
      and array_to_string(problems, ',') ~ '^([a-z][a-z0-9_]{0,63}(,[a-z][a-z0-9_]{0,63})*)?$'
    ),
  actor_kind text not null constraint browser_environment_events_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null constraint browser_environment_events_actor_id_check check (
    btrim(actor_id) <> '' and char_length(actor_id) <= 200
  ),
  correlation_id text not null constraint browser_environment_events_correlation_id_check check (
    btrim(correlation_id) <> '' and char_length(correlation_id) <= 200
  ),
  reason text constraint browser_environment_events_reason_check check (
    btrim(reason) <> '' and char_length(reason) <= 500
  ),
  occurred_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint browser_environment_events_shape_check check (
    (kind = 'transition' and reason is not null and health_ok is null
     and from_status is not null and to_status is not null
     and private.browser_environment_edge_allowed(from_status, to_status))
    or (kind = 'health' and health_ok is not null and from_status is null and to_status is null)
  )
);
create index browser_environment_events_environment_id_idx
  on app.browser_environment_events (environment_id, occurred_at);
create index browser_environment_events_organization_id_idx
  on app.browser_environment_events (organization_id);

create function private.reject_browser_environment_event_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'app.browser_environment_events is append-only' using errcode = '42501';
end;
$$;
create trigger browser_environment_events_append_only
  before update or delete on app.browser_environment_events
  -- Deleting an organization or shop cascades (trigger depth 1); a direct change never passes.
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_browser_environment_event_change();

alter table app.shop_browser_environments enable row level security;
alter table app.browser_environment_events enable row level security;
revoke all on app.shop_browser_environments, app.browser_environment_events
  from public, anon, authenticated, service_role;

-- The Owner and Admins: their organization's environments (no consent or profile columns) and
-- history.
grant select (
  id, organization_id, shop_id, status, challenge_count, last_health_at, last_health_ok,
  last_session_at, created_at, updated_at
) on app.shop_browser_environments to authenticated;
create policy shop_browser_environments_select on app.shop_browser_environments for select
  to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
grant select on app.browser_environment_events to authenticated;
create policy browser_environment_events_select on app.browser_environment_events for select
  to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));

-- The worker reads its job's organization's statuses (the router's browser gate).
grant select (id, organization_id, shop_id, status) on app.shop_browser_environments
  to htco_worker;
create policy shop_browser_environments_worker on app.shop_browser_environments for select
  to htco_worker using (organization_id = (select private.worker_organization_id()));
