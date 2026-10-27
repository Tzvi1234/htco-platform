-- Browser Configuration Profiles (slice 8A.2b, ADR-0025 §2, AC-8A.2.10).
--
-- `app.browser_configuration_profiles` holds every version of each environment's configuration
-- profile: the per-shop settings only, which are the locale and the egress. The platform baseline
-- (the pinned Chromium, the viewport, the color scheme, the permissions policy and the switch
-- list) is the same for every shop and lives in code, so it has no column here. Neither does
-- anything that could make a shop look like another device: there is no User-Agent, screen,
-- time zone, coordinate, extension, proxy or fingerprint column (ADR-0020 §2).
--
-- The rules are database rules, not adapter habits:
-- - a version is append-only, and each new one is exactly the next number for its environment;
-- - the locale is one of the shop's recorded languages, kept with the version (`shop_languages`),
--   so the session start can check it again without reading the marketplace;
-- - the row is bound to its environment, shop and organization by one composite foreign key.
--
-- The API's login role writes a version and its audit row in one transaction, after the Owner or
-- an Admin passed MFA and a step-up (`edit_browser_configuration`). The Owner and Admins read
-- their organization's versions. No other role has any right on the table: the browser worker
-- reads the version it runs with through the leased-job functions of 8A.2c (AC-8A.2.23), not
-- through a grant here.

create table app.browser_configuration_profiles (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid not null,
  environment_id uuid not null,
  version integer not null constraint browser_configuration_profiles_version_check check (
    version >= 1
  ),
  locale text not null constraint browser_configuration_profiles_locale_check check (
    locale ~ '^[a-z]{2,3}(-[A-Z]{2})?$'
  ),
  shop_languages text[] not null constraint browser_configuration_profiles_shop_languages_check check (
    cardinality(shop_languages) between 1 and 20
    and array_to_string(shop_languages, ',')
      ~ '^[a-z]{2,3}(-[A-Z]{2})?(,[a-z]{2,3}(-[A-Z]{2})?)*$'
  ),
  egress_id text not null default 'shared'
    constraint browser_configuration_profiles_egress_id_check check (
      egress_id ~ '^[a-z0-9][a-z0-9_.:-]{0,99}$'
    ),
  actor_kind text not null constraint browser_configuration_profiles_actor_kind_check check (
    actor_kind = 'user'
  ),
  actor_id text not null constraint browser_configuration_profiles_actor_id_check check (
    btrim(actor_id) <> '' and char_length(actor_id) <= 200
  ),
  correlation_id text not null constraint browser_configuration_profiles_correlation_id_check check (
    btrim(correlation_id) <> '' and char_length(correlation_id) <= 200
  ),
  created_at timestamptz not null,
  constraint browser_configuration_profiles_locale_recorded_check check (
    locale = any (shop_languages)
  ),
  constraint browser_configuration_profiles_environment_version_key unique (environment_id, version),
  constraint browser_configuration_profiles_environment_fkey
    foreign key (environment_id, organization_id, shop_id)
    references app.shop_browser_environments (id, organization_id, shop_id) on delete cascade
);
create index browser_configuration_profiles_organization_id_idx
  on app.browser_configuration_profiles (organization_id);

-- A new version is exactly the next number for its environment: no gap, no rewrite of history.
-- Two writers that read the same version both aim at the same next number, and the unique key
-- lets only one of them in.
create function private.browser_configuration_next_version() returns trigger
language plpgsql set search_path = '' as $$
declare
  latest integer;
begin
  select max(p.version) into latest
  from app.browser_configuration_profiles p
  where p.environment_id = new.environment_id;
  if new.version <> coalesce(latest, 0) + 1 then
    -- 40001: a writer that lost the race (or skipped a number) retries from the current one.
    raise exception 'browser configuration: version % is not the next one', new.version
      using errcode = '40001';
  end if;
  return new;
end;
$$;
create trigger browser_configuration_profiles_next_version
  before insert on app.browser_configuration_profiles
  for each row execute function private.browser_configuration_next_version();

create function private.reject_browser_configuration_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'app.browser_configuration_profiles is append-only' using errcode = '42501';
end;
$$;
create trigger browser_configuration_profiles_append_only
  before update or delete on app.browser_configuration_profiles
  -- Deleting an environment, shop or organization cascades (trigger depth 1); a direct change
  -- never passes.
  for each row when (pg_trigger_depth() = 0)
  execute function private.reject_browser_configuration_change();

alter table app.browser_configuration_profiles enable row level security;
revoke all on app.browser_configuration_profiles
  from public, anon, authenticated, service_role, htco_worker, htco_browser;

-- The Owner and Admins read their organization's versions.
grant select on app.browser_configuration_profiles to authenticated;
create policy browser_configuration_profiles_select on app.browser_configuration_profiles
  for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
