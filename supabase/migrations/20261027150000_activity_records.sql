-- Activity records (slice 7.7, ADR-0012 rule 4, ER-AI-16).
--
-- `app.activity_records`: one row per meaningful operation by any actor, the trail that the
-- office, supervision and optimization read. The domain rules of `htco_domain.activity` are
-- checked again here (AC-7.7.2). A summary is a code template's rendering with identifier
-- parameters, and the row names that template (AC-7.7.4): never a prompt, model output or raw
-- external payload. Append-only for every role, even the table owner; only removing the whole
-- organization or shop (a cascade) removes rows.
--
-- Partitioned by month on `occurred_at`. Partitions exist through 2027-12; a month without its
-- partition lands in the default partition, so a write never fails for lack of one (the monthly
-- maintenance job that creates them ahead is a follow-up; see docs/features/activity-records).
--
-- Members read their organization's records, a shop's record only with that shop (AC-7.7.3).
-- The worker appends and reads its job's organization's records. Nobody reaches a partition
-- directly. `task_id` has no foreign key yet: `app.tasks` arrives with slice 7.1.
--
-- Safe on the live database: a new table, functions and policies.

create table app.activity_records (
  id uuid not null,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid,
  kind text not null constraint activity_records_kind_check check (
    kind in ('task_execution', 'automation_execution', 'business_event', 'tool_invocation',
             'message', 'decision', 'approval', 'incident', 'routing_decision',
             'engineering_change', 'reconciliation', 'quality_finding')
  ),
  actor_kind text not null constraint activity_records_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null
    constraint activity_records_actor_id_check check (char_length(btrim(actor_id)) between 1 and 200),
  action text not null constraint activity_records_action_check check (
    char_length(action) <= 100 and action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'
  ),
  outcome text not null constraint activity_records_outcome_check check (
    outcome in ('succeeded', 'failed', 'denied', 'pending')
  ),
  summary_template text not null constraint activity_records_summary_template_check check (
    char_length(summary_template) <= 100 and summary_template ~ '^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$'
  ),
  summary text not null
    constraint activity_records_summary_check check (char_length(btrim(summary)) between 1 and 500),
  occurred_at timestamptz not null,
  correlation_id text not null constraint activity_records_correlation_id_check check (
    char_length(btrim(correlation_id)) between 1 and 200
  ),
  causation_id text constraint activity_records_causation_id_check check (
    char_length(btrim(causation_id)) between 1 and 200
  ),
  task_id uuid,
  created_at timestamptz not null default now(),
  constraint activity_records_pkey primary key (id, occurred_at),
  constraint activity_records_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
) partition by range (occurred_at);

create index activity_records_organization_idx
  on app.activity_records (organization_id, occurred_at desc);
create index activity_records_shop_idx
  on app.activity_records (organization_id, shop_id, occurred_at desc);
create index activity_records_actor_idx
  on app.activity_records (organization_id, actor_kind, actor_id, occurred_at desc);
create index activity_records_correlation_idx
  on app.activity_records (organization_id, correlation_id);

-- One month's partition, with RLS on and no direct grants. Idempotent.
create function private.create_activity_partition(month date) returns void
language plpgsql set search_path = '' as $$
declare
  first_day date := date_trunc('month', month)::date;
  name text := format('activity_records_%s', to_char(first_day, 'YYYY_MM'));
begin
  execute format(
    'create table if not exists app.%I partition of app.activity_records for values from (%L) to (%L)',
    name, first_day::timestamptz, (first_day + interval '1 month')::timestamptz
  );
  execute format('alter table app.%I enable row level security', name);
  execute format('revoke all on app.%I from public, anon, authenticated, service_role, htco_worker', name);
end
$$;
revoke all on function private.create_activity_partition(date) from public;

select private.create_activity_partition(month::date)
from generate_series(date '2026-10-01', date '2027-12-01', interval '1 month') as month;

create table app.activity_records_default partition of app.activity_records default;
alter table app.activity_records_default enable row level security;
revoke all on app.activity_records_default from public, anon, authenticated, service_role, htco_worker;

-- Append-only, except when the organization or the shop itself is removed (depth > 0).
create function private.reject_activity_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'app.activity_records is append-only' using errcode = '42501';
end;
$$;
revoke all on function private.reject_activity_change() from public;
create trigger activity_records_append_only before update or delete on app.activity_records
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_activity_change();

alter table app.activity_records enable row level security;
revoke all on app.activity_records from public, anon, authenticated, service_role;

-- Members: their organization's records; a shop's record only with that shop.
grant select on app.activity_records to authenticated;
create policy activity_records_select on app.activity_records for select to authenticated
  using (
    organization_id = any (array(select private.user_org_ids()))
    and (shop_id is null or shop_id = any (array(select private.user_shop_ids())))
  );

-- The worker: appends and reads its job's organization's records.
grant select, insert on app.activity_records to htco_worker;
create policy activity_records_worker on app.activity_records for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
