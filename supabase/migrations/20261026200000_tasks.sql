-- Business tasks (slice 7.1, FR-TASK-01 to FR-TASK-04, AUD-03).
--
-- `app.tasks`: one row per unit of work for a person, an employee or an automation, of one
-- `kind` (`general`, `human_channel`, `relay`, `approval_follow_up`): a kind adds data, never
-- states. Creating the same task again is a no-op by `(organization_id, idempotency_key)`
-- (AC-7.1.2). The state machine of `htco_domain.tasks` is checked again here, and `completed`
-- needs its evidence (a resource, an execution or an approval; AC-7.1.3).
--
-- `app.task_events`: every recorded transition, append-only.
--
-- Titles are code templates (never buyer data or model text), at most 200 characters. Members
-- read their organization's tasks (a shop's task only with that shop); the worker reads and
-- writes its job's organization only.
--
-- Safe on the live database: new tables, functions and policies.

-- Evidence (AC-7.1.3, sage-arch on #181) names an execution or an approval by its UUID, and a
-- resource as `<kind>:<id>`: the rule of `htco_domain.tasks.TaskEvidence`, inlined in each
-- table's check (the worker has no access to the private schema's helpers).

create table app.tasks (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid,
  kind text not null constraint tasks_kind_check check (
    kind in ('general', 'human_channel', 'relay', 'approval_follow_up')
  ),
  title text not null constraint tasks_title_check check (char_length(btrim(title)) between 1 and 200),
  assignee_kind text not null constraint tasks_assignee_kind_check check (
    assignee_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  assignee_id text not null
    constraint tasks_assignee_id_check check (char_length(assignee_id) between 1 and 200),
  owner_kind text constraint tasks_owner_kind_check check (
    owner_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  owner_id text constraint tasks_owner_id_check check (char_length(owner_id) between 1 and 200),
  state text not null constraint tasks_state_check check (
    state in ('created', 'queued', 'claimed', 'running', 'waiting_for_external',
              'waiting_for_agent', 'waiting_for_approval', 'retry_scheduled', 'blocked',
              'completed', 'failed', 'escalated', 'cancelled')
  ),
  idempotency_key text not null
    constraint tasks_idempotency_key_check check (char_length(btrim(idempotency_key)) between 1 and 200),
  correlation_id text not null
    constraint tasks_correlation_id_check check (char_length(correlation_id) between 1 and 200),
  due_at timestamptz,
  evidence_kind text
    constraint tasks_evidence_kind_check check (evidence_kind in ('resource', 'execution', 'approval')),
  evidence_reference text,
  created_at timestamptz not null,
  updated_at timestamptz not null,
  constraint tasks_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint tasks_owner_check check ((owner_kind is null) = (owner_id is null)),
  constraint tasks_evidence_pair_check check ((evidence_kind is null) = (evidence_reference is null)),
  -- AC-7.1.3: a strict shape, never free text (htco_domain.tasks.TaskEvidence).
  constraint tasks_evidence_reference_check check (
    evidence_reference is null
    or (evidence_kind = 'resource' and evidence_reference ~ '^[a-z][a-z_]{0,40}:[A-Za-z0-9-]{1,100}$')
    or (evidence_kind in ('execution', 'approval')
        and evidence_reference ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
  ),
  -- AC-7.1.3: completed exactly when the evidence is recorded.
  constraint tasks_completed_evidence_check check ((state = 'completed') = (evidence_kind is not null)),
  constraint tasks_idempotency_key unique (organization_id, idempotency_key),
  constraint tasks_id_organization_key unique (id, organization_id)
);
create index tasks_organization_state_idx on app.tasks (organization_id, state, created_at desc);
create index tasks_shop_idx on app.tasks (shop_id);

-- The task machine (htco_domain.tasks), checked again here.
create function private.task_edge_allowed(from_state text, to_state text) returns boolean
language sql immutable set search_path = '' as $$
  select (from_state, to_state) in (
    ('created', 'queued'), ('created', 'cancelled'),
    ('queued', 'claimed'), ('queued', 'cancelled'),
    ('claimed', 'running'), ('claimed', 'queued'),
    ('running', 'waiting_for_external'), ('running', 'waiting_for_agent'),
    ('running', 'waiting_for_approval'), ('running', 'completed'), ('running', 'failed'),
    ('running', 'retry_scheduled'), ('running', 'blocked'), ('running', 'escalated'),
    ('waiting_for_external', 'running'), ('waiting_for_external', 'queued'),
    ('waiting_for_external', 'cancelled'), ('waiting_for_external', 'escalated'),
    ('waiting_for_agent', 'running'), ('waiting_for_agent', 'queued'),
    ('waiting_for_agent', 'cancelled'), ('waiting_for_agent', 'escalated'),
    ('waiting_for_approval', 'running'), ('waiting_for_approval', 'queued'),
    ('waiting_for_approval', 'cancelled'), ('waiting_for_approval', 'escalated'),
    ('retry_scheduled', 'queued'), ('retry_scheduled', 'cancelled'),
    ('blocked', 'queued'), ('blocked', 'escalated'), ('blocked', 'cancelled'),
    ('failed', 'retry_scheduled'), ('failed', 'escalated'),
    ('escalated', 'queued'), ('escalated', 'cancelled')
  )
$$;
revoke all on function private.task_edge_allowed(text, text) from public;

-- Security definer: the worker has no access to the private schema's helpers.
create function private.check_task_state() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if new.state = old.state then
    return new;
  end if;
  if not private.task_edge_allowed(old.state, new.state) then
    raise exception 'task: % -> % is not allowed', old.state, new.state
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_task_state() from public;
create trigger tasks_check_state before update of state on app.tasks
  for each row execute function private.check_task_state();

-- A task starts `created`, without evidence (quill2 on #181): it reaches any other state only
-- through recorded steps, so no insert, the worker's included, puts one in already completed.
create function private.check_task_insert() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if new.state <> 'created' or new.evidence_kind is not null or new.evidence_reference is not null then
    raise exception 'task: a task starts created without evidence, not %', new.state
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_task_insert() from public;
create trigger tasks_check_insert before insert on app.tasks
  for each row execute function private.check_task_insert();

create table app.task_events (
  id uuid primary key,
  organization_id uuid not null,
  task_id uuid not null,
  from_state text,
  to_state text not null,
  actor_kind text not null constraint task_events_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null constraint task_events_actor_id_check check (char_length(actor_id) between 1 and 200),
  occurred_at timestamptz not null,
  evidence_kind text
    constraint task_events_evidence_kind_check check (evidence_kind in ('resource', 'execution', 'approval')),
  evidence_reference text,
  correlation_id text
    constraint task_events_correlation_id_check check (char_length(correlation_id) <= 200),
  created_at timestamptz not null default now(),
  constraint task_events_task_fkey foreign key (task_id, organization_id)
    references app.tasks (id, organization_id) on delete cascade,
  constraint task_events_evidence_pair_check check ((evidence_kind is null) = (evidence_reference is null)),
  constraint task_events_evidence_reference_check check (
    evidence_reference is null
    or (evidence_kind = 'resource' and evidence_reference ~ '^[a-z][a-z_]{0,40}:[A-Za-z0-9-]{1,100}$')
    or (evidence_kind in ('execution', 'approval')
        and evidence_reference ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
  ),
  constraint task_events_completed_evidence_check check ((to_state = 'completed') = (evidence_kind is not null))
);
create index task_events_task_idx on app.task_events (task_id, occurred_at);
create index task_events_organization_id_idx on app.task_events (organization_id);

create function private.check_task_event() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if new.from_state is null then
    if new.to_state <> 'created' then
      raise exception 'task: a task starts created, not %', new.to_state
        using errcode = 'check_violation';
    end if;
  elsif not private.task_edge_allowed(new.from_state, new.to_state) then
    raise exception 'task: % -> % is not allowed', new.from_state, new.to_state
      using errcode = 'check_violation';
  -- A step starts where the task is (sage-arch on #181): the store writes it before it moves the
  -- task, so a step from any other state is a forged or stale history.
  elsif new.from_state is distinct from (
    select t.state from app.tasks t
    where t.id = new.task_id and t.organization_id = new.organization_id
  ) then
    raise exception 'task: a step from % does not start at the task''s state', new.from_state
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.check_task_event() from public;
create trigger task_events_check_edge before insert on app.task_events
  for each row execute function private.check_task_event();

create function private.reject_task_event_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'app.task_events is append-only' using errcode = '42501';
end;
$$;
revoke all on function private.reject_task_event_change() from public;
create trigger task_events_append_only before update or delete on app.task_events
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_task_event_change();

alter table app.tasks enable row level security;
alter table app.task_events enable row level security;
revoke all on app.tasks, app.task_events from public, anon, authenticated, service_role;

-- Members: their organization's shop tasks only with that shop; an organization-wide task (no
-- shop) only for its Owners and Admins (sage-arch on #181).
grant select on app.tasks, app.task_events to authenticated;
create policy tasks_select on app.tasks for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and (
      (shop_id is null and organization_id = any (array(select private.user_admin_org_ids())))
      or shop_id = any (array(select private.user_shop_ids()))
    )
  );
create policy task_events_select on app.task_events for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and exists (
      select 1 from app.tasks t
      where t.id = task_id
        and t.organization_id = task_events.organization_id
        and (
          (t.shop_id is null
           and t.organization_id = any (array(select private.user_admin_org_ids())))
          or t.shop_id = any (array(select private.user_shop_ids()))
        )
    )
  );

-- The worker: its job's organization only.
grant select, insert on app.tasks to htco_worker;
grant update (state, evidence_kind, evidence_reference, updated_at) on app.tasks to htco_worker;
create policy tasks_worker on app.tasks for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

grant select, insert on app.task_events to htco_worker;
create policy task_events_worker on app.task_events for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
