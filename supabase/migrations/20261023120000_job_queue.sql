-- Job queue and worker (slice 5.1, ADR-0007, amended by ADR-0019): an in-house Postgres queue in the
-- `jobs` schema.
--
-- `jobs.jobs` holds at-least-once units of work. A worker claims a due job with
-- `FOR UPDATE SKIP LOCKED`, holds it for a lease that its heartbeat extends, and completes or fails
-- it; a failed job is retried after a backoff with jitter, then ends `dead` (the dead-letter
-- state) with its last error. `(kind, idempotency_key)` is unique, so enqueueing twice creates one
-- job and one scheduled job per period (the scheduler's key names the period). Jobs with the same
-- `lock_key` never run at the same time: the partial unique index below refuses a second running
-- job with that key, whatever the workers believe. `jobs.worker_heartbeats` shows each worker's
-- liveness.
--
-- The worker has its own role, `htco_worker` (no login: the worker's connections switch to it with
-- `set role`, so it never needs a password of its own). It reaches the queue, and the shop tables
-- its jobs need only inside one organization at a time: the worker names the job's organization
-- in the session setting `htco.organization_id`, and the policies below compare every row with it.
-- A job without an organization sees no tenant row at all; the daily fan-out lists shops through
-- two narrow functions that return IDs only. No client role (`anon`, `authenticated`,
-- `service_role`) can see the `jobs` schema.

create schema if not exists jobs;
comment on schema jobs is 'Technical job queue (ADR-0007). Never exposed to clients.';
revoke all on schema jobs from public;
revoke all on schema jobs from anon, authenticated, service_role;

do $$
begin
  if not exists (select from pg_catalog.pg_roles where rolname = 'htco_worker') then
    create role htco_worker nologin noinherit;
  end if;
end
$$;
comment on role htco_worker is
  'The agent-worker (slice 5.1): the queue, and one organization''s shop data per job.';
-- The login role switches to the worker role; it never needs the worker's rights otherwise.
grant htco_worker to postgres with inherit false;
grant usage on schema jobs, app, audit to htco_worker;

-- Jobs ----------------------------------------------------------------------------------------

create table jobs.jobs (
  id uuid primary key,
  kind text not null constraint jobs_kind_check check (
    char_length(kind) <= 64 and kind ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'
  ),
  idempotency_key text not null
    constraint jobs_idempotency_key_check check (char_length(idempotency_key) between 1 and 200),
  organization_id uuid references app.organizations (id) on delete cascade,
  shop_id uuid,
  lock_key text constraint jobs_lock_key_check check (char_length(lock_key) between 1 and 200),
  -- IDs and parameters only: never tokens or buyer data (job-queue security notes).
  payload jsonb not null default '{}'::jsonb constraint jobs_payload_check check (
    jsonb_typeof(payload) = 'object' and pg_column_size(payload) <= 16384
  ),
  state text not null default 'queued' constraint jobs_state_check check (
    state in ('queued', 'running', 'retry_scheduled', 'succeeded', 'dead')
  ),
  priority smallint not null default 0,
  attempts integer not null default 0 constraint jobs_attempts_check check (attempts >= 0),
  max_attempts integer not null
    constraint jobs_max_attempts_check check (max_attempts between 1 and 100),
  initial_backoff_ms bigint not null
    constraint jobs_initial_backoff_ms_check check (initial_backoff_ms >= 0),
  max_backoff_ms bigint not null,
  run_at timestamptz not null,
  leased_by text constraint jobs_leased_by_check check (char_length(leased_by) <= 200),
  lease_expires_at timestamptz,
  -- What started the work and which step caused this job (ADR-0019: carried from the trigger to
  -- the job, the action and the outbox).
  correlation_id text
    constraint jobs_correlation_id_check check (char_length(correlation_id) between 1 and 200),
  causation_id text
    constraint jobs_causation_id_check check (char_length(causation_id) between 1 and 200),
  -- An exception type, or a message the code marked safe to show (no buyer data, no secrets).
  last_error text constraint jobs_last_error_check check (char_length(last_error) <= 500),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz,
  constraint jobs_kind_idempotency_key_key unique (kind, idempotency_key),
  constraint jobs_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint jobs_shop_needs_organization_check check (
    shop_id is null or organization_id is not null
  ),
  constraint jobs_max_backoff_ms_check check (max_backoff_ms >= initial_backoff_ms),
  -- A running job has a holder and a lease; nothing else has either.
  constraint jobs_lease_check check (
    (state = 'running') = (leased_by is not null and lease_expires_at is not null)
  ),
  constraint jobs_finished_at_check check (
    (state in ('succeeded', 'dead')) = (finished_at is not null)
  )
);
-- Claims look for due jobs.
create index jobs_due_idx on jobs.jobs (run_at, priority desc)
  where state in ('queued', 'retry_scheduled');
-- Expired leases are found by their expiry.
create index jobs_lease_expires_at_idx on jobs.jobs (lease_expires_at) where state = 'running';
-- Per-resource locks (AC-5.1.2): at most one running job per lock key.
create unique index jobs_running_lock_key_key on jobs.jobs (lock_key)
  where state = 'running' and lock_key is not null;
-- The dead-letter view, per organization, newest first (AC-5.1.3).
create index jobs_dead_idx on jobs.jobs (organization_id, finished_at desc) where state = 'dead';
create index jobs_organization_id_idx on jobs.jobs (organization_id);
create index jobs_shop_id_idx on jobs.jobs (shop_id);
-- Pruning finished jobs.
create index jobs_finished_at_idx on jobs.jobs (finished_at) where state = 'succeeded';

create trigger jobs_set_updated_at before update on jobs.jobs
  for each row execute function private.set_updated_at();

-- Worker heartbeats ---------------------------------------------------------------------------

create table jobs.worker_heartbeats (
  worker_id text primary key
    constraint worker_heartbeats_worker_id_check check (char_length(worker_id) between 1 and 200),
  release text constraint worker_heartbeats_release_check check (char_length(release) <= 64),
  started_at timestamptz not null,
  last_beat_at timestamptz not null,
  -- No foreign key: finished jobs are pruned, heartbeats stay.
  current_job_id uuid,
  stopping boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index worker_heartbeats_last_beat_at_idx on jobs.worker_heartbeats (last_beat_at desc);

create trigger worker_heartbeats_set_updated_at before update on jobs.worker_heartbeats
  for each row execute function private.set_updated_at();

-- The queue is cross-tenant by nature; only the worker role (and the login role that owns the
-- tables) touches it.
alter table jobs.jobs enable row level security;
alter table jobs.worker_heartbeats enable row level security;
revoke all on jobs.jobs, jobs.worker_heartbeats from public, anon, authenticated, service_role;
grant select, insert, update, delete on jobs.jobs to htco_worker;
grant select, insert, update on jobs.worker_heartbeats to htco_worker;
create policy jobs_worker on jobs.jobs for all to htco_worker using (true) with check (true);
create policy worker_heartbeats_worker on jobs.worker_heartbeats for all to htco_worker
  using (true) with check (true);

-- The worker's tenant scope -------------------------------------------------------------------

-- The organization of the job the worker runs now (null outside a job: no tenant row matches).
create function private.worker_organization_id() returns uuid
language sql stable set search_path = '' as $$
  select nullif(current_setting('htco.organization_id', true), '')::uuid
$$;
revoke all on function private.worker_organization_id() from public, anon, authenticated;
grant execute on function private.worker_organization_id() to htco_worker;

-- Shops: read only.
grant select on app.shops to htco_worker;
create policy shops_worker_select on app.shops for select to htco_worker
  using (organization_id = (select private.worker_organization_id()));

-- Shop integrations: the token provider reads and refreshes tokens, the probes record the last
-- successful call, the expiry check marks `needs_reauth`, the purge clears the purge date.
grant select, update on app.shop_integrations to htco_worker;
create policy shop_integrations_worker_select on app.shop_integrations for select to htco_worker
  using (organization_id = (select private.worker_organization_id()));
create policy shop_integrations_worker_update on app.shop_integrations for update to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- Probe results: written by the daily probes, deleted by the purge.
grant select, insert, update, delete on app.shop_capabilities to htco_worker;
create policy shop_capabilities_worker on app.shop_capabilities for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- Audit: append only, for the job's organization.
grant insert on audit.audit_log to htco_worker;
create policy audit_log_worker_insert on audit.audit_log for insert to htco_worker
  with check (organization_id = (select private.worker_organization_id()));

-- The daily fan-out: which shops exist, by ID only (no tokens, no names, no dates).
create function jobs.connected_shops() returns table (organization_id uuid, shop_id uuid)
language sql stable security definer set search_path = '' as $$
  select i.organization_id, i.shop_id
  from app.shop_integrations i
  where i.status = 'connected'
  order by i.shop_id
$$;
create function jobs.purgeable_shops(p_now timestamptz)
returns table (organization_id uuid, shop_id uuid)
language sql stable security definer set search_path = '' as $$
  select i.organization_id, i.shop_id
  from app.shop_integrations i
  where i.status = 'disconnected' and i.purge_after <= p_now
  order by i.shop_id
$$;
revoke all on function jobs.connected_shops(), jobs.purgeable_shops(timestamptz)
  from public, anon, authenticated, service_role;
grant execute on function jobs.connected_shops(), jobs.purgeable_shops(timestamptz)
  to htco_worker;
