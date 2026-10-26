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
-- - publishes the `browser.environment_status_changed.v1` event of a change it recorded, only
--   through `app.browser_publish_status_change` (no right on the outbox or the queue itself).
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

-- The status event (sage's SECURITY BLOCK on #170): the browser role has no right on
-- `jobs.outbox` or `jobs.jobs` at all. It publishes the event of a status change it has just
-- recorded through this one writer, which builds the message and its delivery job from that
-- change row only, so the role can never name another event type, another tenant's message,
-- a dead message or a crafted job.
create function app.browser_publish_status_change(p_change_id uuid) returns uuid
language plpgsql security definer set search_path = '' as $$
declare
  organization uuid := private.worker_organization_id();
  change app.browser_environment_events%rowtype;
  current_status text;
  message uuid;
  key text;
begin
  -- Only `htco_browser` may execute this (the grant below); it acts in its job's organization.
  if organization is null then
    raise exception 'browser status event: the browser worker in its organization only'
      using errcode = '42501';
  end if;
  select * into change from app.browser_environment_events
  where id = p_change_id and organization_id = organization and kind = 'transition'
    and actor_kind = 'system' and private.browser_role_edge_allowed(from_status, to_status);
  if not found then
    raise exception 'browser status event: no such status change of the browser worker'
      using errcode = '42501';
  end if;
  -- The change is the environment's current status: never an event for a change undone.
  select status into current_status from app.shop_browser_environments
  where id = change.environment_id and organization_id = organization;
  if current_status is distinct from change.to_status then
    raise exception 'browser status event: the environment is no longer %', change.to_status
      using errcode = '42501';
  end if;
  key := 'browser.environment_status_changed.v1:' || change.environment_id || ':'
    || change.to_status || ':' || change.id;
  insert into jobs.outbox
    (id, organization_id, shop_id, destination, idempotency_key, payload, next_attempt_at,
     correlation_id)
  values (
    gen_random_uuid(), organization, change.shop_id, 'events.publish', key,
    jsonb_build_object(
      'event_type', 'browser.environment_status_changed.v1',
      'environment_id', change.environment_id,
      'shop_id', change.shop_id,
      'from_status', change.from_status,
      'to_status', change.to_status,
      'actor', change.actor_kind || ':' || change.actor_id,
      'occurred_at', change.occurred_at
    ),
    now(), change.correlation_id
  )
  on conflict (idempotency_key) do nothing
  returning id into message;
  if message is null then  -- published already: the first message stands, no second job
    select id into message from jobs.outbox where idempotency_key = key;
    return message;
  end if;
  insert into jobs.jobs
    (id, kind, idempotency_key, organization_id, shop_id, payload, max_attempts,
     initial_backoff_ms, max_backoff_ms, run_at, correlation_id)
  values (
    gen_random_uuid(), 'outbox.deliver', 'outbox:' || message, organization, change.shop_id,
    jsonb_build_object('outbox_id', message), 3, 10000, 300000, now(), change.correlation_id
  )
  on conflict (kind, idempotency_key) do nothing;
  return message;
end;
$$;
revoke all on function app.browser_publish_status_change(uuid) from public, anon, authenticated;
grant execute on function app.browser_publish_status_change(uuid) to htco_browser;

-- A dead delivery job marks only its own organization's message dead (Codex P1 on #170).
create or replace function private.sync_outbox_with_job() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  update jobs.outbox
  set next_attempt_at = new.run_at,
      status = case when new.state = 'dead' then 'dead' else status end,
      last_error = case when new.state = 'dead' then coalesce(new.last_error, last_error)
                   else last_error end
  where id = (new.payload ->> 'outbox_id')::uuid and status = 'pending'
    and organization_id is not distinct from new.organization_id;
  return null;
end;
$$;
revoke all on function private.sync_outbox_with_job() from public;
