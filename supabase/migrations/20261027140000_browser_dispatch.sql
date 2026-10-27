-- Browser dispatch on the browser worker's side (slice 8A.2c, ADR-0025 §3 amending ADR-0019;
-- AC-8A.2.18, 2.21, 2.23 to 2.28). Browser runs stay OFF in production until the #95 blockers
-- hold (AC-8A.2.23 merged, the AC-8A.2.29 droplet baseline, and the owner's explicit go).
--
-- 1. `dispatched`: a Browser binding's action waits in this state while the browser worker runs
--    it. The gateway side (moving the action there, the jobs, the resume) is #143's.
-- 2. The lease token. `browser.claim_job` hands the browser worker one due `browser.*` job
--    and an unguessable token, stored only as its SHA-256. Every later call names the job and the
--    token; the token dies with the lease (a trigger clears it whenever the job stops running).
--    The browser role has no right on the queue tables: it claims, renews, completes and fails
--    only through these functions, and only `browser.*` jobs.
-- 3. `app.browser_run_gates`: the flags, scope rows and kill switches the start re-reads. It
--    starts empty, so every run is refused (`flag_off`): fail closed. No role may write it; the
--    Owner's audited, step-up function and its endpoint come in a later PR.
-- 4. `app.browser_run_bindings`: the routed binding stored with the claim (environment, recipe,
--    scope, parameters, request hash, deadline). A job is a locator only; the browser worker
--    runs what is stored here.
-- 5. `app.browser_run_outcomes`: one row per execution and attempt, `started` then final once.
-- 6. `browser.start_run`, `browser.finish_run` and
--    `browser.record_unapproved_read`: the only ways the browser role starts and ends a run.
-- 7. The archives only through a leased job (AC-8A.2.23): the browser role loses its direct grant
--    on `app.browser_profile_archives` and opens, saves and deletes a profile only through
--    `browser.open_profile`, `browser.save_profile` and
--    `browser.delete_profile`, with the generation compare-and-set of ADR-0025 §1.
--
-- Every function here is `security definer`, sets `search_path = ''`, schema-qualifies every
-- name, takes the organization, the execution and the environment from the leased job row (never
-- from an argument or `htco.organization_id`), has EXECUTE revoked from PUBLIC and is granted to
-- `htco_browser` only.

-- The browser role's entry points live in their own schema, which only it can use, so no
-- helper in `private` or `jobs` ever becomes callable by it. (ADR-0025 names them
-- `private.start_browser_run` and `private.finish_browser_run`; they are `browser.start_run` and
-- `browser.finish_run` here, with the same contract.)
create schema browser;
comment on schema browser is 'The browser worker''s entry points (slice 8A.2c). Only htco_browser uses it.';
revoke all on schema browser from public, anon, authenticated, service_role, htco_worker;
alter default privileges in schema browser revoke execute on functions from public;

-- 1. DISPATCHED --------------------------------------------------------------------------------

alter table app.action_executions drop constraint action_executions_state_check;
alter table app.action_executions add constraint action_executions_state_check check (
  state in (
    'received', 'authorized', 'waiting_approval', 'routed', 'dispatched', 'running', 'verifying',
    'succeeded', 'denied', 'rejected', 'expired', 'deferred', 'handed_to_human', 'failed',
    'dead_lettered', 'cancelled'
  )
);

-- 2. The lease token ---------------------------------------------------------------------------

alter table jobs.jobs
  add column lease_token_hash bytea
    constraint jobs_lease_token_hash_check check (
      lease_token_hash is null or octet_length(lease_token_hash) = 32
    );
comment on column jobs.jobs.lease_token_hash is
  'SHA-256 of the browser lease token (slice 8A.2c); cleared whenever the job stops running.';

create function private.clear_job_lease_token() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.state <> 'running' or new.leased_by is distinct from old.leased_by then
    if not (old.state <> 'running' and new.state = 'running') then
      new.lease_token_hash := null;
    end if;
  end if;
  return new;
end;
$$;
create trigger jobs_clear_lease_token before update on jobs.jobs
  for each row execute function private.clear_job_lease_token();

-- An unguessable token: two random UUIDs (244 random bits), hex.
create function private.new_secret_token() returns text
language sql volatile set search_path = '' as $$
  select replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '');
$$;

create function private.token_hash(p_token text) returns bytea
language sql immutable set search_path = '' as $$
  select sha256(convert_to(coalesce(p_token, ''), 'UTF8'));
$$;

-- The leased job behind a call: a running `browser.*` job, leased with this token, lease alive.
create function private.leased_browser_job(p_job_id uuid, p_lease_token text)
returns jobs.jobs
language plpgsql set search_path = '' as $$
declare
  found jobs.jobs;
begin
  select j.* into found from jobs.jobs j
  where j.id = p_job_id
    and j.kind like 'browser.%'
    and j.state = 'running'
    and j.lease_token_hash = private.token_hash(p_lease_token)
    and j.lease_expires_at > now();
  if found.id is null then
    raise exception 'browser job: not leased with this token' using errcode = '42501';
  end if;
  return found;
end;
$$;

create function browser.claim_job(p_worker text, p_lease_seconds integer)
returns table (
  job_id uuid, kind text, organization_id uuid, shop_id uuid, payload jsonb, attempts integer,
  lease_token text, lease_expires_at timestamptz
)
language plpgsql security definer set search_path = '' as $$
#variable_conflict use_column
declare
  token text := private.new_secret_token();
begin
  if p_worker is null or char_length(p_worker) not between 1 and 200 then
    raise exception 'browser job: a worker names itself' using errcode = '22023';
  end if;
  if p_lease_seconds is null or p_lease_seconds not between 5 and 600 then
    raise exception 'browser job: a lease lasts 5 to 600 seconds' using errcode = '22023';
  end if;
  return query
  with candidate as (
    select j.id from jobs.jobs j
    where j.state in ('queued', 'retry_scheduled')
      and j.run_at <= now()
      and j.kind like 'browser.%'
      and (j.lock_key is null or not exists (
        select 1 from jobs.jobs held where held.lock_key = j.lock_key and held.state = 'running'))
    order by j.priority desc, j.run_at, j.id
    limit 1
    for update skip locked
  )
  update jobs.jobs j
  set state = 'running', attempts = j.attempts + 1, leased_by = p_worker,
      lease_expires_at = now() + make_interval(secs => p_lease_seconds),
      lease_token_hash = private.token_hash(token)
  from candidate
  where j.id = candidate.id
  returning j.id, j.kind, j.organization_id, j.shop_id, j.payload, j.attempts, token,
            j.lease_expires_at;
exception
  when unique_violation then
    return;  -- another worker started a job with the same lock key at the same moment
end;
$$;

-- The heartbeat: only while the lease is alive. A stopped heartbeat lets the lease expire, and
-- the queue may redeliver the job; the redelivered job never gets the binding of a started run.
create function browser.renew_lease(p_job_id uuid, p_lease_token text, p_lease_seconds integer)
returns timestamptz
language plpgsql security definer set search_path = '' as $$
declare
  until timestamptz;
begin
  if p_lease_seconds is null or p_lease_seconds not between 5 and 600 then
    raise exception 'browser job: a lease lasts 5 to 600 seconds' using errcode = '22023';
  end if;
  perform private.leased_browser_job(p_job_id, p_lease_token);
  update jobs.jobs set lease_expires_at = now() + make_interval(secs => p_lease_seconds)
  where id = p_job_id
  returning lease_expires_at into until;
  return until;
end;
$$;

create function browser.complete_job(p_job_id uuid, p_lease_token text)
returns void
language plpgsql security definer set search_path = '' as $$
begin
  perform private.leased_browser_job(p_job_id, p_lease_token);
  update jobs.jobs
  set state = 'succeeded', leased_by = null, lease_expires_at = null, finished_at = now(),
      last_error = null
  where id = p_job_id;
end;
$$;

-- A failure goes back to the queue at `p_retry_at`, or ends `dead` (the caller's retry plan).
create function browser.fail_job(
  p_job_id uuid, p_lease_token text, p_error text, p_retry_at timestamptz
)
returns void
language plpgsql security definer set search_path = '' as $$
declare
  job jobs.jobs := private.leased_browser_job(p_job_id, p_lease_token);
  dead boolean := p_retry_at is null or job.attempts >= job.max_attempts;
begin
  update jobs.jobs
  set state = case when dead then 'dead' else 'retry_scheduled' end,
      leased_by = null, lease_expires_at = null, last_error = left(p_error, 500),
      run_at = case when dead then run_at else p_retry_at end,
      finished_at = case when dead then now() end
  where id = p_job_id;
end;
$$;

-- 3. The gates ---------------------------------------------------------------------------------

create table app.browser_run_gates (
  id uuid primary key,
  -- Null only for a platform-wide kill switch.
  organization_id uuid references app.organizations (id) on delete cascade,
  shop_id uuid,
  -- Null: every browser action of that scope.
  action_name text constraint browser_run_gates_action_name_check check (
    char_length(action_name) <= 100 and action_name ~ '^[a-z]+(\.[a-z_]+)+$'
  ),
  kind text not null constraint browser_run_gates_kind_check check (
    kind in ('flag_on', 'scope_recorded', 'kill_switch')
  ),
  scope_id text constraint browser_run_gates_scope_id_check check (
    scope_id ~ '^[A-Z][A-Z0-9]{0,3}-[0-9]{2,3}$'
  ),
  actor_id text not null constraint browser_run_gates_actor_id_check check (
    btrim(actor_id) <> '' and char_length(actor_id) <= 200
  ),
  correlation_id text not null constraint browser_run_gates_correlation_id_check check (
    btrim(correlation_id) <> '' and char_length(correlation_id) <= 200
  ),
  created_at timestamptz not null default now(),
  constraint browser_run_gates_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint browser_run_gates_shape_check check (
    (kind = 'flag_on' and organization_id is not null and shop_id is not null
       and action_name is not null and scope_id is null)
    or (kind = 'scope_recorded' and organization_id is not null and shop_id is not null
       and scope_id is not null and action_name is null)
    or (kind = 'kill_switch' and scope_id is null and (shop_id is null or organization_id is not null))
  )
);
create index browser_run_gates_lookup_idx on app.browser_run_gates (organization_id, shop_id, kind);

alter table app.browser_run_gates enable row level security;
revoke all on app.browser_run_gates
  from public, anon, authenticated, service_role, htco_worker, htco_browser;

-- 4. The bindings ------------------------------------------------------------------------------

create table app.browser_run_bindings (
  action_execution_id uuid not null references app.action_executions (id) on delete cascade,
  attempt integer not null constraint browser_run_bindings_attempt_check check (attempt >= 1),
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  environment_id uuid not null,
  action_name text not null constraint browser_run_bindings_action_name_check check (
    char_length(action_name) <= 100 and action_name ~ '^[a-z]+(\.[a-z_]+)+$'
  ),
  recipe_id text not null constraint browser_run_bindings_recipe_id_check check (
    recipe_id ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$' and char_length(recipe_id) <= 100
  ),
  recipe_version integer not null constraint browser_run_bindings_recipe_version_check check (
    recipe_version >= 1
  ),
  scope_id text constraint browser_run_bindings_scope_id_check check (
    scope_id ~ '^[A-Z][A-Z0-9]{0,3}-[0-9]{2,3}$'
  ),
  -- The parameters the recipe declares; never a secret, a token or buyer contact data.
  parameters jsonb not null default '{}'::jsonb constraint browser_run_bindings_parameters_check check (
    jsonb_typeof(parameters) = 'object' and pg_column_size(parameters) <= 16384
  ),
  request_hash text not null constraint browser_run_bindings_request_hash_check check (
    request_hash ~ '^[0-9a-f]{64}$'
  ),
  needs_approval boolean not null,
  is_write boolean not null,
  automation_id text constraint browser_run_bindings_automation_id_check check (
    char_length(automation_id) <= 100
  ),
  deadline timestamptz not null,
  created_at timestamptz not null default now(),
  primary key (action_execution_id, attempt),
  constraint browser_run_bindings_environment_fkey foreign key (environment_id, organization_id, shop_id)
    references app.shop_browser_environments (id, organization_id, shop_id) on delete cascade,
  -- Every Etsy browser action needs an approval each time (D-16); a write always does.
  constraint browser_run_bindings_write_check check (not is_write or needs_approval)
);
create index browser_run_bindings_organization_id_idx on app.browser_run_bindings (organization_id);
create index browser_run_bindings_environment_id_idx on app.browser_run_bindings (environment_id);

create function private.reject_browser_run_binding_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'app.browser_run_bindings is append-only' using errcode = '42501';
end;
$$;
create trigger browser_run_bindings_append_only
  before update or delete on app.browser_run_bindings
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_browser_run_binding_change();

alter table app.browser_run_bindings enable row level security;
revoke all on app.browser_run_bindings
  from public, anon, authenticated, service_role, htco_worker, htco_browser;
-- The agent-worker's dispatch (#143) stores the binding with the claim, for its job's organization.
grant select, insert on app.browser_run_bindings to htco_worker;
create policy browser_run_bindings_worker on app.browser_run_bindings for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- 5. The outcomes ------------------------------------------------------------------------------

create table app.browser_run_outcomes (
  action_execution_id uuid not null,
  attempt integer not null,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  environment_id uuid not null,
  status text not null constraint browser_run_outcomes_status_check check (
    status in ('started', 'succeeded', 'failed', 'challenge', 'refused', 'timeout', 'interrupted')
  ),
  error_code text constraint browser_run_outcomes_error_code_check check (
    error_code ~ '^[a-z][a-z0-9_]{0,63}$'
  ),
  -- The recipe's validated output (DC1 only where its output model declares it); never a secret.
  output jsonb constraint browser_run_outcomes_output_check check (
    output is null or (jsonb_typeof(output) = 'object' and pg_column_size(output) <= 65536)
  ),
  evidence jsonb not null default '[]'::jsonb constraint browser_run_outcomes_evidence_check check (
    jsonb_typeof(evidence) = 'array' and pg_column_size(evidence) <= 8192
  ),
  run_token_hash bytea constraint browser_run_outcomes_run_token_hash_check check (
    run_token_hash is null or octet_length(run_token_hash) = 32
  ),
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  primary key (action_execution_id, attempt),
  constraint browser_run_outcomes_binding_fkey foreign key (action_execution_id, attempt)
    references app.browser_run_bindings (action_execution_id, attempt) on delete cascade,
  constraint browser_run_outcomes_final_check check ((status = 'started') = (finished_at is null)),
  constraint browser_run_outcomes_started_check check (
    status <> 'started' or (started_at is not null and run_token_hash is not null)
  )
);
create unique index browser_run_outcomes_run_token_hash_key on app.browser_run_outcomes (run_token_hash)
  where run_token_hash is not null;
-- One live session per profile (AC-8A.2.25): at most one started run per environment.
create unique index browser_run_outcomes_live_key on app.browser_run_outcomes (environment_id)
  where status = 'started';
create index browser_run_outcomes_organization_id_idx on app.browser_run_outcomes (organization_id);

-- A final status is final; only `started` moves, once.
create function private.check_browser_run_outcome_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'app.browser_run_outcomes keeps every run' using errcode = '42501';
  end if;
  if old.status <> 'started' then
    raise exception 'browser run: % is final', old.status using errcode = '23514';
  end if;
  if new.action_execution_id <> old.action_execution_id or new.attempt <> old.attempt
     or new.organization_id <> old.organization_id or new.environment_id <> old.environment_id
     or new.started_at is distinct from old.started_at then
    raise exception 'browser run: only its outcome changes' using errcode = '23514';
  end if;
  return new;
end;
$$;
create trigger browser_run_outcomes_check_change
  before update or delete on app.browser_run_outcomes
  for each row when (pg_trigger_depth() = 0)
  execute function private.check_browser_run_outcome_change();

alter table app.browser_run_outcomes enable row level security;
revoke all on app.browser_run_outcomes
  from public, anon, authenticated, service_role, htco_worker, htco_browser;
-- The agent-worker's resume and watchdog (#143) read their job's organization's outcomes.
grant select (
  action_execution_id, attempt, organization_id, shop_id, environment_id, status, error_code,
  output, evidence, started_at, finished_at, created_at
) on app.browser_run_outcomes to htco_worker;
create policy browser_run_outcomes_worker on app.browser_run_outcomes for select to htco_worker
  using (organization_id = (select private.worker_organization_id()));

-- 6. Starting and finishing a run -------------------------------------------------------------

-- Whether this backend holds the profile's advisory lock (the controller's `ProfileLocks`).
create function private.holds_browser_profile_lock(p_environment_id uuid) returns boolean
language sql stable set search_path = '' as $$
  with k as (
    select hashtextextended('htco.browser_profile:' || p_environment_id::text, 0) as key
  )
  select exists (
    select from pg_catalog.pg_locks l, k
    where l.locktype = 'advisory' and l.granted and l.objsubid = 1
      and l.pid = pg_catalog.pg_backend_pid()
      and l.classid = ((k.key >> 32) & 4294967295)::oid
      and l.objid = (k.key & 4294967295)::oid
  );
$$;

create function private.publish_browser_run_finished(
  p_job jobs.jobs, p_execution uuid, p_attempt integer, p_status text, p_error text
) returns void
language sql set search_path = '' as $$
  insert into jobs.outbox
    (id, organization_id, shop_id, destination, idempotency_key, payload, next_attempt_at,
     action_execution_id, correlation_id)
  values (
    gen_random_uuid(), p_job.organization_id, p_job.shop_id, 'events.publish',
    'browser.run_finished.v1:' || p_execution::text || ':' || p_attempt::text,
    jsonb_build_object(
      'event_type', 'browser.run_finished.v1', 'action_execution_id', p_execution::text,
      'attempt', p_attempt, 'status', p_status, 'error_code', p_error
    ),
    now(), p_execution, p_job.correlation_id
  )
  on conflict (idempotency_key) do nothing;
$$;

-- The refusal a start ends with: a final row (once) and its event; no page loads.
create function private.refuse_browser_run(
  p_job jobs.jobs, p_binding app.browser_run_bindings, p_code text
) returns void
language plpgsql set search_path = '' as $$
begin
  insert into app.browser_run_outcomes
    (action_execution_id, attempt, organization_id, shop_id, environment_id, status, error_code,
     finished_at)
  values (p_binding.action_execution_id, p_binding.attempt, p_binding.organization_id,
          p_binding.shop_id, p_binding.environment_id, 'refused', p_code, now())
  on conflict (action_execution_id, attempt) do nothing;
  if found then
    perform private.publish_browser_run_finished(
      p_job, p_binding.action_execution_id, p_binding.attempt, 'refused', p_code);
  end if;
end;
$$;

-- The binding of the leased `browser.run` job: the execution and attempt from the job's payload,
-- the organization from the job row; a binding of another organization does not exist.
create function private.leased_browser_run_binding(p_job jobs.jobs)
returns app.browser_run_bindings
language plpgsql set search_path = '' as $$
declare
  found app.browser_run_bindings;
  v_execution uuid;
  v_attempt integer;
begin
  if p_job.kind <> 'browser.run' then
    raise exception 'browser job: not a browser.run job' using errcode = '42501';
  end if;
  begin
    v_execution := (p_job.payload ->> 'action_execution_id')::uuid;
    v_attempt := (p_job.payload ->> 'attempt')::integer;
  exception when others then
    raise exception 'browser job: its payload is not a locator' using errcode = '22023';
  end;
  select b.* into found from app.browser_run_bindings b
  where b.action_execution_id = v_execution and b.attempt = v_attempt
    and b.organization_id = p_job.organization_id
    and b.shop_id is not distinct from p_job.shop_id;
  if found.action_execution_id is null then
    raise exception 'browser job: no binding for this job' using errcode = '42501';
  end if;
  return found;
end;
$$;

-- The gate that refuses a run now, or null. Takes the gates table in SHARE mode first, so a gate
-- change (which writes the table) and a start are serialized: one sees the other (AC-8A.2.26).
create function private.browser_run_gate_refusal(p_binding app.browser_run_bindings) returns text
language plpgsql set search_path = '' as $$
begin
  lock table app.browser_run_gates in share mode;
  if exists (
    select from app.browser_run_gates g
    where g.kind = 'kill_switch'
      and (g.organization_id is null or g.organization_id = p_binding.organization_id)
      and (g.shop_id is null or g.shop_id = p_binding.shop_id)
      and (g.action_name is null or g.action_name = p_binding.action_name)
  ) then
    return 'kill_switch';
  end if;
  if not exists (
    select from app.browser_run_gates g
    where g.kind = 'flag_on' and g.organization_id = p_binding.organization_id
      and g.shop_id = p_binding.shop_id and g.action_name = p_binding.action_name
  ) then
    return 'flag_off';
  end if;
  if p_binding.scope_id is not null and not exists (
    select from app.browser_run_gates g
    where g.kind = 'scope_recorded' and g.organization_id = p_binding.organization_id
      and g.shop_id = p_binding.shop_id and g.scope_id = p_binding.scope_id
  ) then
    return 'scope_not_recorded';
  end if;
  if p_binding.automation_id is not null and not exists (
    select from app.automation_definitions d
    where d.id = p_binding.automation_id and d.readiness = 'active'
  ) then
    return 'automation_not_ready';
  end if;
  return null;
end;
$$;

-- Why a start or an unapproved read is refused now, or null: the execution, its request, the
-- deadline and the environment, then the gates. One list for both (Quill on #202), so a read that
-- records itself directly is held to every check a start is.
create function private.browser_run_refusal(
  p_binding app.browser_run_bindings, p_execution app.action_executions, p_environment_status text
) returns text
language plpgsql set search_path = '' as $$
begin
  return coalesce(
    case
      when p_execution.state <> 'dispatched' then 'execution_not_dispatched'
      when p_execution.request_hash <> p_binding.request_hash then 'request_hash_mismatch'
      when p_binding.deadline <= now() then 'deadline_passed'
      when p_environment_status is distinct from 'ready' then 'environment_not_ready'
    end,
    private.browser_run_gate_refusal(p_binding)
  );
end;
$$;

-- AC-8A.2.18, 2.19, 2.21, 2.25, 2.26, 2.28. Call it on the profile lock's own connection.
-- Returns one row with `outcome = 'started'` and the binding and a fresh run token, or
-- `outcome = 'refused'` with its code; returns no row at all when the attempt was already
-- started (a redelivered job gets nothing, not even the binding).
create function browser.start_run(p_job_id uuid, p_lease_token text)
returns table (
  outcome text, refusal text, run_token text, action_execution_id uuid, attempt integer,
  environment_id uuid, action_name text, recipe_id text, recipe_version integer, scope_id text,
  parameters jsonb, request_hash text, is_write boolean, deadline timestamptz
)
language plpgsql security definer set search_path = '' as $$
#variable_conflict use_column
declare
  job jobs.jobs := private.leased_browser_job(p_job_id, p_lease_token);
  binding app.browser_run_bindings := private.leased_browser_run_binding(job);
  execution app.action_executions;
  environment_status text;
  code text;
  token text;
  created integer;
begin
  -- The marker is committed under the profile lock only (AC-8A.2.28).
  if not private.holds_browser_profile_lock(binding.environment_id) then
    raise exception 'browser run: start it under the profile lock' using errcode = '42501';
  end if;
  -- Cancellation and start are serialized on the action execution's row (AC-8A.2.15).
  select e.* into execution from app.action_executions e
  where e.id = binding.action_execution_id for update;
  if execution.id is null or execution.organization_id <> job.organization_id then
    raise exception 'browser run: the execution is not the job''s' using errcode = '42501';
  end if;
  -- A redelivered job for an attempt with any row gets nothing and touches nothing.
  if exists (
    select from app.browser_run_outcomes o
    where o.action_execution_id = binding.action_execution_id and o.attempt = binding.attempt
  ) then
    return;
  end if;
  select e.status into environment_status from app.shop_browser_environments e
  where e.id = binding.environment_id for update;
  -- One live session per profile, even under a partition (AC-8A.2.25): wait for it to resolve.
  if exists (
    select from app.browser_run_outcomes o
    where o.environment_id = binding.environment_id and o.status = 'started'
  ) then
    raise exception 'browser run: another run of this profile is live' using errcode = '55P03';
  end if;
  code := private.browser_run_refusal(binding, execution, environment_status);
  -- Approvals are not in Postgres before Phase 7 (7.3, 7.11): an approval-gated run fails closed.
  if code is null and binding.needs_approval then
    code := 'approval_unavailable';
  end if;
  if code is not null then
    perform private.refuse_browser_run(job, binding, code);
    return query select 'refused'::text, code, null::text, binding.action_execution_id,
      binding.attempt, binding.environment_id, null::text, null::text, null::integer, null::text,
      null::jsonb, null::text, null::boolean, null::timestamptz;
    return;
  end if;
  token := private.new_secret_token();
  insert into app.browser_run_outcomes
    (action_execution_id, attempt, organization_id, shop_id, environment_id, status,
     run_token_hash, started_at)
  values (binding.action_execution_id, binding.attempt, binding.organization_id,
          binding.shop_id, binding.environment_id, 'started', private.token_hash(token), now())
  on conflict do nothing
  returning 1 into created;
  if created is null then
    return;  -- someone else created the row: they started it
  end if;
  return query select 'started'::text, null::text, token, binding.action_execution_id,
    binding.attempt, binding.environment_id, binding.action_name, binding.recipe_id,
    binding.recipe_version, binding.scope_id, binding.parameters, binding.request_hash,
    binding.is_write, binding.deadline;
end;
$$;

-- AC-8A.2.21: records the outcome of a started run by its run token, even after the job's lease
-- expired; once; never `interrupted` (only the watchdog sets that). True when it was recorded.
create function browser.finish_run(
  p_run_token text, p_status text, p_error_code text, p_output jsonb, p_evidence jsonb
) returns boolean
language plpgsql security definer set search_path = '' as $$
declare
  finished app.browser_run_outcomes;
  job jobs.jobs;
begin
  if p_status not in ('succeeded', 'failed', 'challenge', 'refused', 'timeout') then
    raise exception 'browser run: % is not an outcome the worker records', p_status
      using errcode = '22023';
  end if;
  update app.browser_run_outcomes o
  set status = p_status, error_code = p_error_code, output = p_output,
      evidence = coalesce(p_evidence, '[]'::jsonb), finished_at = now(), run_token_hash = null
  where o.run_token_hash = private.token_hash(p_run_token) and o.status = 'started'
  returning o.* into finished;
  if finished.action_execution_id is null then
    return false;  -- a forged, used or other attempt's token
  end if;
  select j.* into job from jobs.jobs j
  where j.kind = 'browser.run' and j.organization_id = finished.organization_id
    and j.payload ->> 'action_execution_id' = finished.action_execution_id::text
    and (j.payload ->> 'attempt')::integer = finished.attempt
  limit 1;
  if job.id is null then
    job.organization_id := finished.organization_id;
    job.shop_id := finished.shop_id;
  end if;
  perform private.publish_browser_run_finished(
    job, finished.action_execution_id, finished.attempt, p_status, p_error_code);
  return true;
end;
$$;

-- AC-8A.2.24: a read that needs no approval gets its final row directly, once, with its leased
-- job; the same checks as a start, under the profile lock. A refused read records only its
-- refusal (a final row with the code, once) and returns false, as a start that is refused does.
create function browser.record_unapproved_read(
  p_job_id uuid, p_lease_token text, p_status text, p_error_code text, p_output jsonb,
  p_evidence jsonb
) returns boolean
language plpgsql security definer set search_path = '' as $$
declare
  job jobs.jobs := private.leased_browser_job(p_job_id, p_lease_token);
  binding app.browser_run_bindings := private.leased_browser_run_binding(job);
  execution app.action_executions;
  environment_status text;
  code text;
begin
  if binding.needs_approval or binding.is_write then
    raise exception 'browser run: only an unapproved read records directly' using errcode = '42501';
  end if;
  if p_status not in ('succeeded', 'failed', 'challenge', 'refused', 'timeout') then
    raise exception 'browser run: % is not an outcome the worker records', p_status
      using errcode = '22023';
  end if;
  if not private.holds_browser_profile_lock(binding.environment_id) then
    raise exception 'browser run: record it under the profile lock' using errcode = '42501';
  end if;
  -- The same checks as a start, on the same rows (Quill on #202): flag, kill switch, scope,
  -- readiness, deadline, the execution's state and request, and the environment.
  select e.* into execution from app.action_executions e
  where e.id = binding.action_execution_id for update;
  if execution.id is null or execution.organization_id <> job.organization_id then
    raise exception 'browser run: the execution is not the job''s' using errcode = '42501';
  end if;
  select e.status into environment_status from app.shop_browser_environments e
  where e.id = binding.environment_id for update;
  code := private.browser_run_refusal(binding, execution, environment_status);
  if code is not null then
    perform private.refuse_browser_run(job, binding, code);
    return false;
  end if;
  insert into app.browser_run_outcomes
    (action_execution_id, attempt, organization_id, shop_id, environment_id, status, error_code,
     output, evidence, finished_at)
  values (binding.action_execution_id, binding.attempt, binding.organization_id, binding.shop_id,
          binding.environment_id, p_status, p_error_code, p_output,
          coalesce(p_evidence, '[]'::jsonb), now())
  on conflict (action_execution_id, attempt) do nothing;
  if not found then
    return false;
  end if;
  perform private.publish_browser_run_finished(
    job, binding.action_execution_id, binding.attempt, p_status, p_error_code);
  return true;
end;
$$;

-- 7. The archives through a leased job (AC-8A.2.23) -------------------------------------------

-- The environment of the leased `browser.*` job: its shop's one environment, in its organization.
create function private.leased_browser_environment(p_job jobs.jobs)
returns app.shop_browser_environments
language plpgsql set search_path = '' as $$
declare
  found app.shop_browser_environments;
begin
  select e.* into found from app.shop_browser_environments e
  where e.shop_id = p_job.shop_id and e.organization_id = p_job.organization_id;
  if found.id is null then
    raise exception 'browser job: its shop has no environment' using errcode = '42501';
  end if;
  return found;
end;
$$;

create function browser.open_profile(p_job_id uuid, p_lease_token text)
returns table (
  environment_id uuid, organization_id uuid, shop_id uuid, key_id text, archive bytea,
  sealed_at timestamptz, generation bigint
)
language plpgsql security definer set search_path = '' as $$
#variable_conflict use_column
declare
  environment app.shop_browser_environments :=
    private.leased_browser_environment(private.leased_browser_job(p_job_id, p_lease_token));
begin
  return query
  select a.environment_id, a.organization_id, a.shop_id, a.key_id, a.archive, a.sealed_at,
         a.generation
  from app.browser_profile_archives a
  where a.environment_id = environment.id and a.organization_id = environment.organization_id;
end;
$$;

-- Writes the archive and the environment's record of it. `p_expected_generation`: the
-- generation the session read (compare-and-set), 0 for "none existed"; null only to provision a
-- profile that has no archive yet (an existing one is never overwritten that way). The session's
-- write-back also runs on its profile lock's own connection, which checks the lock in the same
-- transaction (#169); the compare-and-set is the fence everywhere else. A stale write raises
-- 40001 and leaves nothing. Returns the new generation.
create function browser.save_profile(
  p_job_id uuid, p_lease_token text, p_key_id text, p_archive bytea, p_sealed_at timestamptz,
  p_storage_key text, p_expected_generation bigint
) returns bigint
language plpgsql security definer set search_path = '' as $$
declare
  environment app.shop_browser_environments :=
    private.leased_browser_environment(private.leased_browser_job(p_job_id, p_lease_token));
  written bigint;
begin

  update app.shop_browser_environments
  set profile_storage_key = p_storage_key, profile_key_id = p_key_id
  where id = environment.id;
  if p_expected_generation is null or p_expected_generation = 0 then
    insert into app.browser_profile_archives
      (environment_id, organization_id, shop_id, key_id, archive, sealed_at)
    values (environment.id, environment.organization_id, environment.shop_id, p_key_id,
            p_archive, p_sealed_at)
    on conflict (environment_id) do nothing
    returning generation into written;
  else
    update app.browser_profile_archives
    set key_id = p_key_id, archive = p_archive, sealed_at = p_sealed_at,
        generation = p_expected_generation + 1
    where environment_id = environment.id and generation = p_expected_generation
    returning generation into written;
  end if;
  if written is null then
    -- Stale: nothing of this call stays (the record update is rolled back with it).
    raise exception 'browser profile: the archive changed since it was read'
      using errcode = '40001';
  end if;
  return written;
end;
$$;

create function browser.delete_profile(p_job_id uuid, p_lease_token text)
returns void
language plpgsql security definer set search_path = '' as $$
declare
  environment app.shop_browser_environments :=
    private.leased_browser_environment(private.leased_browser_job(p_job_id, p_lease_token));
begin
  -- Deleting needs no lock check here: a session still running afterwards read a generation
  -- that no longer exists, so its compare-and-set write-back is refused.
  update app.shop_browser_environments
  set profile_storage_key = null, profile_key_id = null
  where id = environment.id;
  delete from app.browser_profile_archives where environment_id = environment.id;
end;
$$;

-- The browser role reaches the archives only through those functions now.
revoke all on app.browser_profile_archives from htco_browser;
drop policy browser_profile_archives_browser on app.browser_profile_archives;
-- Its profile record is written by `save_browser_profile` and `delete_browser_profile` only.
revoke update (profile_storage_key, profile_key_id) on app.shop_browser_environments
  from htco_browser;

-- 8. Who may call what ------------------------------------------------------------------------

do $$
declare
  fn text;
begin
  foreach fn in array array[
    'private.clear_job_lease_token()',
    'private.new_secret_token()',
    'private.token_hash(text)',
    'private.leased_browser_job(uuid, text)',
    'private.holds_browser_profile_lock(uuid)',
    'private.publish_browser_run_finished(jobs.jobs, uuid, integer, text, text)',
    'private.refuse_browser_run(jobs.jobs, app.browser_run_bindings, text)',
    'private.leased_browser_run_binding(jobs.jobs)',
    'private.browser_run_gate_refusal(app.browser_run_bindings)',
    'private.browser_run_refusal(app.browser_run_bindings, app.action_executions, text)',
    'private.leased_browser_environment(jobs.jobs)',
    'private.check_browser_run_outcome_change()',
    'private.reject_browser_run_binding_change()',
    'browser.claim_job(text, integer)',
    'browser.renew_lease(uuid, text, integer)',
    'browser.complete_job(uuid, text)',
    'browser.fail_job(uuid, text, text, timestamptz)',
    'browser.start_run(uuid, text)',
    'browser.finish_run(text, text, text, jsonb, jsonb)',
    'browser.record_unapproved_read(uuid, text, text, text, jsonb, jsonb)',
    'browser.open_profile(uuid, text)',
    'browser.save_profile(uuid, text, text, bytea, timestamptz, text, bigint)',
    'browser.delete_profile(uuid, text)'
  ] loop
    execute format('revoke all on function %s from public, anon, authenticated, service_role', fn);
  end loop;
end
$$;

grant usage on schema browser to htco_browser;
grant execute on function
  browser.claim_job(text, integer),
  browser.renew_lease(uuid, text, integer),
  browser.complete_job(uuid, text),
  browser.fail_job(uuid, text, text, timestamptz),
  browser.start_run(uuid, text),
  browser.finish_run(text, text, text, jsonb, jsonb),
  browser.record_unapproved_read(uuid, text, text, text, jsonb, jsonb),
  browser.open_profile(uuid, text),
  browser.save_profile(uuid, text, text, bytea, timestamptz, text, bigint),
  browser.delete_profile(uuid, text)
to htco_browser;
