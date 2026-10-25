-- The browser worker's role and the sealed profile archives (slice 8A.1, ADR-0020 §2 and §8).
--
-- `htco_browser` is the browser worker's own role (no login: its connections switch to it with
-- `set role`, like `htco_worker`). It reaches one organization at a time, named in the session
-- setting `htco.organization_id`: its environments (status and profile columns, never the
-- consent record), their sealed profile archives, and audit inserts. It has no right on
-- `app.shop_integrations` or any token, and no right on another organization's rows
-- (AC-8A.1.2). Status transitions, the history and the queue are granted with the slices that
-- use them (8A.2 to 8A.4), not before.
--
-- `app.browser_profile_archives` holds each environment's sealed profile: a gzip tar of the
-- Chromium profile, sealed with envelope encryption under the browser profile key ring, bound
-- to `org|shop|environment`. The database cannot open it; it only keeps it, at most 25 MiB, for
-- the browser role alone (main's decision of 2026-10-25: archives in Postgres, so the browser
-- worker never holds a project-wide Storage key). No client role sees the table at all.

do $$
begin
  if not exists (select from pg_catalog.pg_roles where rolname = 'htco_browser') then
    create role htco_browser nologin noinherit;
  end if;
end
$$;
comment on role htco_browser is
  'The browser worker (slice 8A.1): one organization''s browser environments and profiles.';
grant htco_browser to postgres with inherit false;
grant usage on schema app, audit to htco_browser;
grant execute on function private.worker_organization_id() to htco_browser;

-- An archive's row names the environment, its shop and its organization, all three bound.
alter table app.shop_browser_environments
  add constraint shop_browser_environments_identity_key unique (id, organization_id, shop_id);

create table app.browser_profile_archives (
  environment_id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  key_id text not null constraint browser_profile_archives_key_id_check check (
    key_id ~ '^[A-Za-z0-9._-]{1,32}$'
  ),
  -- The sealed blob only (version, key ID, wrapped data key, ciphertext): never a plain profile.
  archive bytea not null constraint browser_profile_archives_archive_check check (
    octet_length(archive) between 1 and 26214400
  ),
  sealed_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint browser_profile_archives_environment_fkey
    foreign key (environment_id, organization_id, shop_id)
    references app.shop_browser_environments (id, organization_id, shop_id) on delete cascade
);
-- Sealed bytes do not compress: keep them out of line, uncompressed.
alter table app.browser_profile_archives alter column archive set storage external;
create index browser_profile_archives_organization_id_idx
  on app.browser_profile_archives (organization_id);

create trigger browser_profile_archives_set_updated_at
  before update on app.browser_profile_archives
  for each row execute function private.set_updated_at();

alter table app.browser_profile_archives enable row level security;
revoke all on app.browser_profile_archives from public, anon, authenticated, service_role;

grant select, insert, update, delete on app.browser_profile_archives to htco_browser;
create policy browser_profile_archives_browser on app.browser_profile_archives for all
  to htco_browser
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- Its organization's environments: the status and the profile record, never the consent.
grant select (
  id, organization_id, shop_id, status, challenge_count, last_health_at, last_health_ok,
  last_session_at, profile_storage_key, profile_key_id
) on app.shop_browser_environments to htco_browser;
grant update (profile_storage_key, profile_key_id, last_session_at)
  on app.shop_browser_environments to htco_browser;
create policy shop_browser_environments_browser_select on app.shop_browser_environments
  for select to htco_browser
  using (organization_id = (select private.worker_organization_id()));
create policy shop_browser_environments_browser_update on app.shop_browser_environments
  for update to htco_browser
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- Audit: append only, for its organization.
grant insert on audit.audit_log to htco_browser;
create policy audit_log_browser_insert on audit.audit_log for insert to htco_browser
  with check (organization_id = (select private.worker_organization_id()));
