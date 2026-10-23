-- The Action Gateway's executions (slice 5.12, ADR-0019, execution-engine.md §4 and §7).
--
-- One row per business effect: unique `(organization_id, action_name, idempotency_key)`, so a
-- replayed or concurrent submission finds the first row and runs nothing again. The row keeps the
-- state, channel, verification, evidence references, actor, correlation and causation IDs, the
-- approval and the outcome. Outputs are kept only for actions without personal data (R0 reads and
-- writes); an R1 read's output, which may hold buyer data, is never stored. Finished rows never
-- change (a trigger refuses it).
--
-- The API's login role writes; the worker reaches its job's organization's rows; signed-in users
-- read the executions of their organization (Owner, Admin) or of their granted shops.

create table app.action_executions (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid,
  action_name text not null constraint action_executions_action_name_check check (
    char_length(action_name) <= 100 and action_name ~ '^[a-z]+(\.[a-z_]+)+$'
  ),
  action_version integer not null constraint action_executions_action_version_check check (
    action_version >= 1
  ),
  idempotency_key text not null constraint action_executions_idempotency_key_check check (
    char_length(idempotency_key) between 1 and 200
  ),
  request_hash text not null
    constraint action_executions_request_hash_check check (request_hash ~ '^[0-9a-f]{64}$'),
  state text not null constraint action_executions_state_check check (
    state in (
      'received', 'authorized', 'waiting_approval', 'routed', 'running', 'verifying',
      'succeeded', 'denied', 'rejected', 'expired', 'deferred', 'handed_to_human', 'failed',
      'dead_lettered', 'cancelled'
    )
  ),
  outcome text constraint action_executions_outcome_check check (
    outcome in (
      'succeeded', 'failed', 'denied', 'pending_approval', 'handed_to_human', 'deferred'
    )
  ),
  channel text constraint action_executions_channel_check check (
    channel in ('api', 'browser', 'internal', 'human')
  ),
  attempts integer not null default 0 constraint action_executions_attempts_check check (
    attempts >= 0
  ),
  verified boolean not null default false,
  error_code text constraint action_executions_error_code_check check (
    char_length(error_code) <= 64
  ),
  -- Why it was refused or failed: codes and short texts, never the input or a secret.
  reason text constraint action_executions_reason_check check (char_length(reason) <= 500),
  output jsonb constraint action_executions_output_check check (
    output is null or (jsonb_typeof(output) = 'object' and pg_column_size(output) <= 65536)
  ),
  evidence jsonb not null default '[]'::jsonb
    constraint action_executions_evidence_check check (jsonb_typeof(evidence) = 'array'),
  actor_kind text not null constraint action_executions_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null constraint action_executions_actor_id_check check (
    btrim(actor_id) <> '' and char_length(actor_id) <= 200
  ),
  on_behalf_of_user uuid,
  correlation_id text not null constraint action_executions_correlation_id_check check (
    char_length(correlation_id) between 1 and 200
  ),
  causation_id text constraint action_executions_causation_id_check check (
    char_length(causation_id) <= 200
  ),
  approval_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz,
  constraint action_executions_key_key unique (organization_id, action_name, idempotency_key),
  constraint action_executions_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
);
create index action_executions_organization_id_id_idx
  on app.action_executions (organization_id, id desc);
create index action_executions_shop_id_idx on app.action_executions (shop_id);
create index action_executions_waiting_idx on app.action_executions (organization_id)
  where state in ('waiting_approval', 'handed_to_human');

create trigger action_executions_set_updated_at before update on app.action_executions
  for each row execute function private.set_updated_at();

-- Finished executions are history: their state never changes again.
create function private.check_action_execution_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  if old.state in ('succeeded', 'denied', 'rejected', 'expired', 'dead_lettered', 'cancelled')
     and (new.state is distinct from old.state or new.outcome is distinct from old.outcome
          or new.output is distinct from old.output) then
    raise exception 'action execution: % is final', old.state using errcode = '23514';
  end if;
  return new;
end;
$$;
create trigger action_executions_check_change before update on app.action_executions
  for each row execute function private.check_action_execution_change();

-- The outbox message of an action now references its row (the column came with slice 5.2).
alter table jobs.outbox
  add constraint outbox_action_execution_fkey foreign key (action_execution_id)
  references app.action_executions (id) on delete set null not valid;

alter table app.action_executions enable row level security;
revoke all on app.action_executions from public, anon, authenticated, service_role;

grant select (
  id, organization_id, shop_id, action_name, action_version, idempotency_key, state, outcome,
  channel, attempts, verified, error_code, actor_kind, actor_id, correlation_id, causation_id,
  approval_id, created_at, updated_at, finished_at
) on app.action_executions to authenticated;
create policy action_executions_select on app.action_executions for select to authenticated
  using (
    organization_id = any (array(select private.user_admin_org_ids()))
    or shop_id = any (array(select private.user_shop_ids()))
  );

grant select, insert, update on app.action_executions to htco_worker;
create policy action_executions_worker on app.action_executions for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
