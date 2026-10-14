-- Identity and access with row-level security (slice 2.2; DATA_MODEL §3-§4, PERMISSION_MATRIX §1).
--
-- Organizations are the tenant root. A membership gives a user one role in one organization;
-- Owner and Admin reach every shop of their organization, every other role only the shops granted
-- in `shop_access`. The API reads these tables inside user-scoped transactions as `authenticated`,
-- so RLS is the second line behind the policy engine. Writes to owner-administered tables also
-- need an `aal2` session (MFA). The audit log is append-only for everyone, its owner included.

-- Helpers shared by the tables ------------------------------------------------------------------

create function private.set_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- Tables ---------------------------------------------------------------------------------------

create table app.organizations (
  id uuid primary key,
  name text not null constraint organizations_name_check check (char_length(btrim(name)) between 1 and 200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table app.user_profiles (
  user_id uuid primary key references auth.users (id) on delete cascade,
  display_name text not null constraint user_profiles_display_name_check check (char_length(btrim(display_name)) between 1 and 200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table app.memberships (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  role text not null constraint memberships_role_check check (
    role in ('owner', 'admin', 'operations', 'finance', 'support', 'developer', 'viewer')
  ),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  revoked_at timestamptz,
  constraint memberships_id_organization_id_key unique (id, organization_id)
);
-- One active membership per user and organization; revoked ones stay for history.
create unique index memberships_organization_id_user_id_key
  on app.memberships (organization_id, user_id) where revoked_at is null;
create index memberships_user_id_idx on app.memberships (user_id);

create table app.shops (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  name text not null constraint shops_name_check check (char_length(btrim(name)) between 1 and 200),
  marketplace text not null constraint shops_marketplace_check check (marketplace in ('etsy')),
  status text not null constraint shops_status_check check (status in ('pending', 'active', 'disconnected')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint shops_id_organization_id_key unique (id, organization_id)
);
create index shops_organization_id_idx on app.shops (organization_id);

-- The composite foreign keys keep a grant inside one organization: the membership and the shop
-- must both belong to `organization_id`.
create table app.shop_access (
  id uuid primary key,
  organization_id uuid not null,
  membership_id uuid not null,
  shop_id uuid not null,
  created_at timestamptz not null default now(),
  constraint shop_access_membership_fkey foreign key (membership_id, organization_id)
    references app.memberships (id, organization_id) on delete cascade,
  constraint shop_access_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint shop_access_membership_id_shop_id_key unique (membership_id, shop_id)
);
create index shop_access_organization_id_idx on app.shop_access (organization_id);
create index shop_access_shop_id_idx on app.shop_access (shop_id);

-- No foreign key to organizations: the audit trail outlives what it describes.
create table audit.audit_log (
  id uuid primary key,
  occurred_at timestamptz not null default now(),
  organization_id uuid not null,
  actor_kind text not null constraint audit_log_actor_kind_check check (
    actor_kind in ('user', 'employee', 'automation', 'advisor', 'system')
  ),
  actor_id text not null constraint audit_log_actor_id_check check (btrim(actor_id) <> ''),
  action text not null,
  entity_type text not null,
  entity_id text not null,
  before jsonb,
  after jsonb,
  correlation_id text
);
create index audit_log_organization_id_occurred_at_idx on audit.audit_log (organization_id, occurred_at desc);

create trigger organizations_set_updated_at before update on app.organizations
  for each row execute function private.set_updated_at();
create trigger user_profiles_set_updated_at before update on app.user_profiles
  for each row execute function private.set_updated_at();
create trigger memberships_set_updated_at before update on app.memberships
  for each row execute function private.set_updated_at();
create trigger shops_set_updated_at before update on app.shops
  for each row execute function private.set_updated_at();

-- Append-only, even for the table owner (grants cover every other role).
create function private.reject_audit_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'audit.audit_log is append-only' using errcode = '42501';
end;
$$;
create trigger audit_log_append_only before update or delete on audit.audit_log
  for each row execute function private.reject_audit_change();
create trigger audit_log_no_truncate before truncate on audit.audit_log
  for each statement execute function private.reject_audit_change();

-- RLS helpers: security definer so policies can read memberships without recursing into their
-- own policies; `search_path = ''` so every name is qualified. Callable only from policies.

create function private.user_org_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.organization_id
  from app.memberships m
  where m.user_id = (select auth.uid()) and m.revoked_at is null
$$;

create function private.user_admin_org_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.organization_id
  from app.memberships m
  where m.user_id = (select auth.uid()) and m.revoked_at is null and m.role in ('owner', 'admin')
$$;

create function private.user_membership_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.id
  from app.memberships m
  where m.user_id = (select auth.uid()) and m.revoked_at is null
$$;

-- Owner and Admin memberships see every shop of their organization; other roles see granted shops.
create function private.user_shop_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select s.id
  from app.memberships m
  join app.shops s on s.organization_id = m.organization_id
  where m.user_id = (select auth.uid()) and m.revoked_at is null and m.role in ('owner', 'admin')
  union
  select sa.shop_id
  from app.shop_access sa
  join app.memberships m on m.id = sa.membership_id
  where m.user_id = (select auth.uid()) and m.revoked_at is null
$$;

-- Users who share an organization with the caller (their profiles are visible, DC3).
create function private.coworker_user_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select other.user_id
  from app.memberships mine
  join app.memberships other on other.organization_id = mine.organization_id and other.revoked_at is null
  where mine.user_id = (select auth.uid()) and mine.revoked_at is null
$$;

-- True when the session verified a second factor.
create function private.session_is_aal2() returns boolean
language sql stable security definer set search_path = '' as $$
  select coalesce((select auth.jwt()) ->> 'aal', '') = 'aal2'
$$;

revoke all on all functions in schema private from public, anon, authenticated;
grant execute on function
  private.user_org_ids(), private.user_admin_org_ids(), private.user_membership_ids(),
  private.user_shop_ids(), private.coworker_user_ids(), private.session_is_aal2()
  to authenticated;

-- Grants: exactly what each role needs ---------------------------------------------------------

revoke all on all tables in schema app, audit from public, anon, authenticated, service_role;

grant select on app.organizations, app.user_profiles, app.memberships, app.shops, app.shop_access
  to authenticated;
grant update (name) on app.organizations to authenticated;
grant insert (user_id, display_name), update (display_name) on app.user_profiles to authenticated;
grant insert, update (role, revoked_at), delete on app.memberships to authenticated;
grant insert, update (name, status) on app.shops to authenticated;
grant insert, delete on app.shop_access to authenticated;
grant select, insert on audit.audit_log to authenticated;

-- Administrative tooling (the bootstrap command, migrations checks); never the request path.
grant select, insert, update, delete on all tables in schema app to service_role;
grant select, insert on audit.audit_log to service_role;

-- Row-level security ---------------------------------------------------------------------------

alter table app.organizations enable row level security;
alter table app.user_profiles enable row level security;
alter table app.memberships enable row level security;
alter table app.shops enable row level security;
alter table app.shop_access enable row level security;
alter table audit.audit_log enable row level security;

-- organizations
create policy organizations_select on app.organizations for select to authenticated
  using (id = any (array(select private.user_org_ids())));
create policy organizations_update on app.organizations for update to authenticated
  using (id = any (array(select private.user_admin_org_ids())))
  with check (id = any (array(select private.user_admin_org_ids())));
create policy organizations_update_aal2 on app.organizations as restrictive for update to authenticated
  using ((select private.session_is_aal2())) with check ((select private.session_is_aal2()));

-- user_profiles: your own, and those of people in your organizations; you edit only your own.
create policy user_profiles_select on app.user_profiles for select to authenticated
  using (user_id = (select auth.uid()) or user_id = any (array(select private.coworker_user_ids())));
create policy user_profiles_insert on app.user_profiles for insert to authenticated
  with check (user_id = (select auth.uid()));
create policy user_profiles_update on app.user_profiles for update to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

-- memberships: members see the organization's members; Owner and Admin manage them with MFA.
create policy memberships_select on app.memberships for select to authenticated
  using (organization_id = any (array(select private.user_org_ids())));
create policy memberships_insert on app.memberships for insert to authenticated
  with check (organization_id = any (array(select private.user_admin_org_ids())));
create policy memberships_update on app.memberships for update to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())))
  with check (organization_id = any (array(select private.user_admin_org_ids())));
create policy memberships_delete on app.memberships for delete to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
create policy memberships_insert_aal2 on app.memberships as restrictive for insert to authenticated
  with check ((select private.session_is_aal2()));
create policy memberships_update_aal2 on app.memberships as restrictive for update to authenticated
  using ((select private.session_is_aal2())) with check ((select private.session_is_aal2()));
create policy memberships_delete_aal2 on app.memberships as restrictive for delete to authenticated
  using ((select private.session_is_aal2()));

-- shops
create policy shops_select on app.shops for select to authenticated
  using (id = any (array(select private.user_shop_ids())));
create policy shops_insert on app.shops for insert to authenticated
  with check (organization_id = any (array(select private.user_admin_org_ids())));
create policy shops_update on app.shops for update to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())))
  with check (organization_id = any (array(select private.user_admin_org_ids())));
create policy shops_insert_aal2 on app.shops as restrictive for insert to authenticated
  with check ((select private.session_is_aal2()));
create policy shops_update_aal2 on app.shops as restrictive for update to authenticated
  using ((select private.session_is_aal2())) with check ((select private.session_is_aal2()));

-- shop_access: Owner and Admin see and manage every grant; a member sees its own grants.
create policy shop_access_select on app.shop_access for select to authenticated
  using (
    organization_id = any (array(select private.user_admin_org_ids()))
    or membership_id = any (array(select private.user_membership_ids()))
  );
create policy shop_access_insert on app.shop_access for insert to authenticated
  with check (organization_id = any (array(select private.user_admin_org_ids())));
create policy shop_access_delete on app.shop_access for delete to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
create policy shop_access_insert_aal2 on app.shop_access as restrictive for insert to authenticated
  with check ((select private.session_is_aal2()));
create policy shop_access_delete_aal2 on app.shop_access as restrictive for delete to authenticated
  using ((select private.session_is_aal2()));

-- audit_log: members append records for their own organizations; Owner and Admin read them.
create policy audit_log_select on audit.audit_log for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
create policy audit_log_insert on audit.audit_log for insert to authenticated
  with check (organization_id = any (array(select private.user_org_ids())));
