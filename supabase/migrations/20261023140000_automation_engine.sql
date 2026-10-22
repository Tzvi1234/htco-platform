-- The durable automation engine (slice 5.10, ADR-0013).
--
-- `app.automation_definitions` holds every version of every automation in `registry/automations/`,
-- loaded only by the reviewed sync command (`htco registry sync --automations`, applied by the
-- migration workflow) with the registry commit it came from; rows are never edited by hand. The
-- current definition is the highest version of an ID.
--
-- `app.automation_executions` is one row per `(automation_id, idempotency_key)`: a replayed trigger
-- finds the first execution (AC-5.10.1). A trigger rejects any state change outside the
-- `ExecutionState` machine, even from SQL (AC-5.10.6, defense in depth for the domain's own check),
-- and `app.automation_execution_events` keeps every change with its actor and correlation ID,
-- append-only.
--
-- The API writes with its login role after the policy engine allowed the caller; signed-in users
-- may read executions of their organization (Owner, Admin) or of their granted shops. The worker
-- reads definitions and reaches executions and events of its job's organization only.

create table app.automation_definitions (
  id text not null constraint automation_definitions_id_check check (
    id ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)+$' and char_length(id) <= 64
  ),
  version integer not null constraint automation_definitions_version_check check (version >= 1),
  name text not null constraint automation_definitions_name_check check (char_length(name) <= 200),
  business_owner text not null,
  primary_trigger text not null constraint automation_definitions_primary_trigger_check check (
    primary_trigger in ('event', 'schedule', 'agent_tool', 'user_action', 'workflow')
  ),
  callable_from text[] not null constraint automation_definitions_callable_from_check check (
    callable_from <@ array['event', 'schedule', 'agent_tool', 'user_action', 'workflow']
    and primary_trigger = any (callable_from)
  ),
  retry_max_attempts integer not null check (retry_max_attempts >= 1),
  retry_initial_backoff_seconds integer not null check (retry_initial_backoff_seconds >= 0),
  retry_max_backoff_seconds integer not null,
  timeout_seconds integer not null check (timeout_seconds > 0),
  side_effect text not null,
  approval_required boolean not null,
  ai_steps text[] not null default '{}',
  readiness text not null constraint automation_definitions_readiness_check check (
    readiness in ('draft', 'specified', 'evaluating', 'ready', 'active', 'suspended', 'retired')
  ),
  activation_phase integer not null check (activation_phase >= 0),
  user_capability text,
  -- The 18 sections, as reviewed.
  specification jsonb not null constraint automation_definitions_specification_check check (
    jsonb_typeof(specification) = 'object'
  ),
  -- The whole reviewed document, from which the engine rebuilds the definition.
  document jsonb not null constraint automation_definitions_document_check check (
    jsonb_typeof(document) = 'object'
  ),
  -- SHA-256 (hex) of the whole document, so the sync sees any change.
  specification_hash text not null
    constraint automation_definitions_specification_hash_check check (
      specification_hash ~ '^[0-9a-f]{64}$'
    ),
  registry_commit text not null
    constraint automation_definitions_registry_commit_check check (
      registry_commit ~ '^[0-9a-f]{7,40}$|^local$'
    ),
  synced_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint automation_definitions_pkey primary key (id, version),
  constraint automation_definitions_backoff_check check (
    retry_max_backoff_seconds >= retry_initial_backoff_seconds
  )
);

create trigger automation_definitions_set_updated_at before update on app.automation_definitions
  for each row execute function private.set_updated_at();

create table app.automation_executions (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid,
  automation_id text not null,
  automation_version integer not null,
  idempotency_key text not null constraint automation_executions_idempotency_key_check check (
    char_length(idempotency_key) between 1 and 300
  ),
  trigger text not null constraint automation_executions_trigger_check check (
    trigger in ('event', 'schedule', 'agent_tool', 'user_action', 'workflow')
  ),
  actor_kind text not null constraint automation_executions_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null constraint automation_executions_actor_id_check check (
    btrim(actor_id) <> '' and char_length(actor_id) <= 200
  ),
  state text not null constraint automation_executions_state_check check (
    state in (
      'pending', 'running', 'waiting', 'succeeded', 'failed', 'retry_scheduled',
      'dead_lettered', 'compensated', 'cancelled'
    )
  ),
  attempts integer not null default 0 constraint automation_executions_attempts_check check (
    attempts >= 0
  ),
  correlation_id text not null
    constraint automation_executions_correlation_id_check check (char_length(correlation_id) <= 200),
  causation_id text
    constraint automation_executions_causation_id_check check (char_length(causation_id) <= 200),
  last_error text constraint automation_executions_last_error_check check (
    char_length(last_error) <= 500
  ),
  -- IDs and parameters of the trigger; never tokens or buyer contact data.
  payload jsonb not null default '{}'::jsonb constraint automation_executions_payload_check check (
    jsonb_typeof(payload) = 'object' and pg_column_size(payload) <= 16384
  ),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint automation_executions_automation_id_idempotency_key_key
    unique (automation_id, idempotency_key),
  constraint automation_executions_definition_fkey foreign key (automation_id, automation_version)
    references app.automation_definitions (id, version),
  constraint automation_executions_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
);
create index automation_executions_organization_id_automation_id_id_idx
  on app.automation_executions (organization_id, automation_id, id desc);
create index automation_executions_shop_id_idx on app.automation_executions (shop_id);
create index automation_executions_dead_idx on app.automation_executions (organization_id)
  where state = 'dead_lettered';

create trigger automation_executions_set_updated_at before update on app.automation_executions
  for each row execute function private.set_updated_at();

-- The ExecutionState machine (htco_domain.automation), checked again by the database.
create function private.check_execution_transition() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.state = old.state then
    return new;
  end if;
  if (old.state, new.state) not in (
    ('pending', 'running'), ('pending', 'cancelled'),
    ('running', 'succeeded'), ('running', 'failed'),
    ('failed', 'retry_scheduled'), ('failed', 'dead_lettered'), ('failed', 'compensated'),
    ('retry_scheduled', 'running'), ('retry_scheduled', 'cancelled'),
    ('dead_lettered', 'retry_scheduled'),
    -- Waiting for an approval or a person (ADR-0019): resumed, or ended.
    ('running', 'waiting'), ('waiting', 'retry_scheduled'), ('waiting', 'cancelled'),
    -- One stored step may span two edges of the machine (failed is never at rest).
    ('running', 'retry_scheduled'), ('running', 'dead_lettered')
  ) then
    raise exception 'automation execution: % -> % is not allowed', old.state, new.state
      using errcode = '23514';
  end if;
  return new;
end;
$$;
create trigger automation_executions_check_transition before update of state
  on app.automation_executions
  for each row execute function private.check_execution_transition();

create table app.automation_execution_events (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  execution_id uuid not null references app.automation_executions (id) on delete cascade,
  from_state text,
  to_state text not null,
  actor_kind text not null constraint automation_execution_events_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null,
  correlation_id text not null,
  reason text constraint automation_execution_events_reason_check check (char_length(reason) <= 500),
  occurred_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index automation_execution_events_execution_id_idx
  on app.automation_execution_events (execution_id, occurred_at);
create index automation_execution_events_organization_id_idx
  on app.automation_execution_events (organization_id);

create function private.reject_execution_event_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'app.automation_execution_events is append-only' using errcode = '42501';
end;
$$;
create trigger automation_execution_events_append_only
  before update or delete on app.automation_execution_events
  -- Deleting an organization cascades (trigger depth 1); a direct change never passes.
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_execution_event_change();

alter table app.automation_definitions enable row level security;
alter table app.automation_executions enable row level security;
alter table app.automation_execution_events enable row level security;
revoke all on app.automation_definitions, app.automation_executions,
  app.automation_execution_events from public, anon, authenticated, service_role;

-- Signed-in users: read executions of their organization (Owner, Admin) or of granted shops.
grant select on app.automation_executions to authenticated;
create policy automation_executions_select on app.automation_executions for select
  to authenticated
  using (
    organization_id = any (array(select private.user_admin_org_ids()))
    or shop_id = any (array(select private.user_shop_ids()))
  );

-- The worker: definitions, and its job's organization's executions and events.
grant select on app.automation_definitions to htco_worker;
create policy automation_definitions_worker on app.automation_definitions for select
  to htco_worker using (true);
grant select, update on app.automation_executions to htco_worker;
create policy automation_executions_worker_select on app.automation_executions for select
  to htco_worker using (organization_id = (select private.worker_organization_id()));
create policy automation_executions_worker_update on app.automation_executions for update
  to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
grant select, insert on app.automation_execution_events to htco_worker;
create policy automation_execution_events_worker on app.automation_execution_events for all
  to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
