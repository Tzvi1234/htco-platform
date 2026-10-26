-- Browser sessions and the environment's error counter (slice 8A.2, ADR-0020 §3).
--
-- `app.browser_sessions` records every Browser Controller session: its environment, a stable
-- purpose code, when it started and ended, and its outcome (`running`, then `succeeded`,
-- `failed` with a failure code such as `timeout`, or `deferred` when another session held the
-- profile past its deadline). A failure is a code, never free text that could carry page
-- content. An ended session is final. The recipe, execution, preflight and challenge columns
-- arrive with the slices that fill them (8A.3 to 8A.5).
--
-- `shop_browser_environments.error_count` counts failed sessions (AC-8A.2.3); with
-- `last_session_at` it is the controller's only write to the environment row.
--
-- The browser role writes its organization's sessions and these two columns only; the Owner
-- and Admins read their organization's sessions (the environment screens, 8A.9).

alter table app.shop_browser_environments
  add column error_count integer not null default 0
    constraint shop_browser_environments_error_count_check check (error_count >= 0);
grant select (error_count) on app.shop_browser_environments to authenticated, htco_browser;
grant update (error_count) on app.shop_browser_environments to htco_browser;

create table app.browser_sessions (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  environment_id uuid not null,
  purpose text not null constraint browser_sessions_purpose_check check (
    char_length(purpose) <= 64 and purpose ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'
  ),
  outcome text not null default 'running' constraint browser_sessions_outcome_check check (
    outcome in ('running', 'succeeded', 'failed', 'deferred')
  ),
  failure text constraint browser_sessions_failure_check check (
    failure ~ '^[a-z][a-z0-9_]{0,63}$'
  ),
  started_at timestamptz not null,
  ended_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint browser_sessions_environment_fkey
    foreign key (environment_id, organization_id, shop_id)
    references app.shop_browser_environments (id, organization_id, shop_id) on delete cascade,
  constraint browser_sessions_shape_check check (
    (outcome = 'running' and ended_at is null and failure is null)
    or (outcome in ('succeeded', 'deferred') and ended_at is not null and failure is null)
    or (outcome = 'failed' and ended_at is not null and failure is not null)
  ),
  constraint browser_sessions_ended_check check (ended_at is null or ended_at >= started_at)
);
create index browser_sessions_environment_id_idx
  on app.browser_sessions (environment_id, started_at desc);
create index browser_sessions_organization_id_idx on app.browser_sessions (organization_id);

create trigger browser_sessions_set_updated_at
  before update on app.browser_sessions
  for each row execute function private.set_updated_at();

-- An ended session is final; a running one only ends.
create function private.check_browser_session_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  if old.outcome <> 'running' then
    raise exception 'browser session: an ended session is final' using errcode = '23514';
  end if;
  if (new.id, new.organization_id, new.shop_id, new.environment_id, new.purpose,
      new.started_at) is distinct from
     (old.id, old.organization_id, old.shop_id, old.environment_id, old.purpose,
      old.started_at) then
    raise exception 'browser session: only the outcome and end change' using errcode = '23514';
  end if;
  return new;
end;
$$;
create trigger browser_sessions_check_change
  before update on app.browser_sessions
  for each row execute function private.check_browser_session_change();

alter table app.browser_sessions enable row level security;
revoke all on app.browser_sessions from public, anon, authenticated, service_role;

grant select, insert, update on app.browser_sessions to htco_browser;
create policy browser_sessions_browser on app.browser_sessions for all to htco_browser
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

grant select on app.browser_sessions to authenticated;
create policy browser_sessions_select on app.browser_sessions for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
