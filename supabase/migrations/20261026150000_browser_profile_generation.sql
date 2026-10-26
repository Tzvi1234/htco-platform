-- The profile archive's generation and the environment's provider (slice 8A.2a, ADR-0025 §1).
--
-- 1. `app.browser_profile_archives.generation`: a compare-and-set counter. Each write of an
--    archive moves it up by exactly one, and the write names the generation it read. A session
--    that opened generation N can only replace generation N, so a stale profile never replaces
--    a newer one, even if the lease fence (#169, the write on the lock's own connection) were
--    bypassed. The trigger makes this a database rule, not only the adapter's habit: an insert
--    starts at 1 and an update must name the next generation.
--    (Agreed with #169/#172: that PR fenced the write on the lease's connection, with no
--    migration; this column is the compare-and-set half of ADR-0025's fenced write-back.)
--
-- 2. `app.shop_browser_environments.provider`: the `BrowserProvider` that runs the environment,
--    from the registry `app.browser_providers` with its readiness. Only `managed-chromium`
--    exists, and it is `enabled`. No role is granted a write on the column or the registry:
--    changing a provider is the Owner's, with aal2 and step-up and audited (AC-8A.2.6), and
--    lands with its endpoint. Until then the column only reads.

-- 1. The generation --------------------------------------------------------------------------

alter table app.browser_profile_archives
  add column generation bigint not null default 1
    constraint browser_profile_archives_generation_check check (generation >= 1);

comment on column app.browser_profile_archives.generation is
  'Compare-and-set counter: 1 on insert, exactly +1 on each update (ADR-0025 §1).';

create function private.browser_profile_archive_generation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' and new.generation <> 1 then
    raise exception 'a new profile archive starts at generation 1'
      using errcode = 'check_violation';
  end if;
  if tg_op = 'UPDATE' and new.generation <> old.generation + 1 then
    raise exception 'a profile archive write must name the next generation'
      using errcode = 'check_violation';
  end if;
  return new;
end
$$;
revoke all on function private.browser_profile_archive_generation() from public;

create trigger browser_profile_archives_generation
  before insert or update on app.browser_profile_archives
  for each row execute function private.browser_profile_archive_generation();

-- 2. The provider ----------------------------------------------------------------------------

create table app.browser_providers (
  id text primary key constraint browser_providers_id_check check (
    id ~ '^[a-z][a-z0-9-]{1,63}$'
  ),
  readiness text not null constraint browser_providers_readiness_check check (
    readiness in ('enabled', 'disabled')
  ),
  created_at timestamptz not null default now()
);
comment on table app.browser_providers is
  'The browser providers and their readiness (ADR-0025 §1). Only managed-chromium exists.';

insert into app.browser_providers (id, readiness) values ('managed-chromium', 'enabled');

-- No role reads or writes the registry yet: the Owner's provider change (AC-8A.2.6) brings the
-- one function that reads it.
alter table app.browser_providers enable row level security;
revoke all on app.browser_providers from public, anon, authenticated, service_role;

alter table app.shop_browser_environments
  add column provider text not null default 'managed-chromium'
    constraint shop_browser_environments_provider_fkey
      references app.browser_providers (id);

comment on column app.shop_browser_environments.provider is
  'The environment''s BrowserProvider; only the Owner changes it (AC-8A.2.6).';

-- Read only, for the roles that already read the environment.
grant select (provider) on app.shop_browser_environments
  to authenticated, htco_browser, htco_worker;
