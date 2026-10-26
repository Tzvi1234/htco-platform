-- The browser worker's login and its health rights (slice 8A.3, ADR-0020 §4 and §5).
--
-- `htco_browser_login` is the browser worker's own login (sage's binding review of #116,
-- ADR-0020's validation log): it can connect, and it is a member of `htco_browser` only, so
-- its connections `set role htco_browser`, and a `reset role` lands on a login that holds no
-- right of its own. It never connects as `postgres` or the owner. Its password is never in a
-- migration: it is set out of band, and until then the role cannot sign in at all.
--
-- Health (AC-8A.3.1 to AC-8A.3.3): in its own organization, the browser role now
-- - records health reports (the environment's `last_health_*` columns and a `health` row in
--   its history),
-- - moves the status only along the health and sign-in edges (`ready` <-> `degraded`,
--   `ready` or `provisioning` -> `login_required`), each with its `transition` history row,
-- - writes the `browser.environment_status_changed.v1` outbox message and its delivery job.
-- Its history rows are always the system's (`actor_kind = 'system'`), never a person's. It
-- still never sees or writes the consent record, and nothing else changes.

do $$
begin
  if not exists (select from pg_catalog.pg_roles where rolname = 'htco_browser_login') then
    create role htco_browser_login login noinherit nosuperuser nocreatedb nocreaterole
      noreplication nobypassrls;
  end if;
end
$$;
comment on role htco_browser_login is
  'The browser worker''s login (slice 8A.3): switches to htco_browser; no right of its own.';
-- Never a password here (the repository is public); set it out of band.
alter role htco_browser_login password null;
grant htco_browser to htco_browser_login with inherit false, set true;

-- Health reports and the status, in its own organization.
grant update (status, last_health_at, last_health_ok)
  on app.shop_browser_environments to htco_browser;

-- The browser role moves only the health and sign-in edges. Every other change (provisioning,
-- suspension, deletion, a person's sign-in) belongs to the API and to people.
create function private.browser_role_edge_allowed(from_status text, to_status text)
returns boolean
language sql immutable set search_path = '' as $$
  select (from_status, to_status) in (
    ('ready', 'degraded'), ('degraded', 'ready'),
    ('ready', 'login_required'), ('provisioning', 'login_required')
  );
$$;

-- The status trigger calls `private` helpers, which the browser role cannot see (it has no
-- usage on that schema), so it now runs with its owner's rights. It only checks the edge.
alter function private.check_browser_environment_status() security definer;

-- The browser role's own edges, checked with the caller's rights (`current_user` is the role
-- that changes the row), and with the list inline, so nothing in `private` is needed.
create function private.check_browser_role_status() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.status <> old.status and current_user = 'htco_browser'
     and (old.status, new.status) not in (
       ('ready', 'degraded'), ('degraded', 'ready'),
       ('ready', 'login_required'), ('provisioning', 'login_required')
     ) then
    raise exception 'browser environment: the browser worker cannot move % -> %',
      old.status, new.status using errcode = '42501';
  end if;
  return new;
end;
$$;
create trigger shop_browser_environments_check_browser_role
  before update of status on app.shop_browser_environments
  for each row execute function private.check_browser_role_status();

-- The history: the latest health results (read) and its own system rows (append).
grant select, insert on app.browser_environment_events to htco_browser;
create policy browser_environment_events_browser_select on app.browser_environment_events
  for select to htco_browser
  using (organization_id = (select private.worker_organization_id()));
create policy browser_environment_events_browser_insert on app.browser_environment_events
  for insert to htco_browser
  with check (
    organization_id = (select private.worker_organization_id())
    and actor_kind = 'system'
    and (kind = 'health'
         or private.browser_role_edge_allowed(from_status, to_status))
  );

-- The status event: its outbox message and that message's delivery job, nothing else.
grant usage on schema jobs to htco_browser;
grant insert on jobs.outbox to htco_browser;
grant select (id, organization_id, idempotency_key, destination) on jobs.outbox
  to htco_browser;
create policy outbox_browser_insert on jobs.outbox for insert to htco_browser
  with check (
    organization_id = (select private.worker_organization_id())
    and destination = 'events.publish'
  );
create policy outbox_browser_select on jobs.outbox for select to htco_browser
  using (
    organization_id = (select private.worker_organization_id())
    and destination = 'events.publish'
  );
grant insert on jobs.jobs to htco_browser;
grant select (id, kind, idempotency_key, organization_id) on jobs.jobs to htco_browser;
create policy jobs_browser_insert on jobs.jobs for insert to htco_browser
  with check (
    organization_id = (select private.worker_organization_id()) and kind = 'outbox.deliver'
  );
create policy jobs_browser_select on jobs.jobs for select to htco_browser
  using (
    organization_id = (select private.worker_organization_id()) and kind = 'outbox.deliver'
  );
