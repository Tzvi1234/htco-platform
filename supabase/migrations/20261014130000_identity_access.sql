-- Identity and access (phase 2, slice 2.2; DATA_MODEL §3 and §4; PERMISSION_MATRIX §1).
-- Every table gets row-level security in this migration. Clients never reach these tables
-- through the Data API (only `public` is exposed); the HTCO API runs user requests as
-- `authenticated` with the verified JWT claims, so these policies hold even if a query forgets
-- a filter. Writes are limited to Owners and Admins and need an MFA session (aal2).

-- Roles of a membership (htco_domain.identity.Role).
create domain app.member_role as text
  check (value in ('owner', 'admin', 'operations', 'finance', 'support', 'developer', 'viewer'));

-- Actor kinds of an audit record (htco_domain.actors.ActorKind).
create domain audit.actor_kind as text
  check (value in ('user', 'employee', 'automation', 'advisor', 'system'));

create table app.organizations (
  id uuid primary key,
  name text not null check (length(btrim(name)) between 1 and 200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Profile data of a Supabase Auth user (DC3: visible inside the user's organizations).
create table app.user_profiles (
  user_id uuid primary key references auth.users (id) on delete cascade,
  display_name text check (display_name is null or length(btrim(display_name)) between 1 and 200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table app.memberships (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  role app.member_role not null,
  created_at timestamptz not null default now(),
  revoked_at timestamptz,
  constraint memberships_organization_id_user_id_key unique (organization_id, user_id),
  -- Lets shop_access prove that its membership and its shop belong to one organization.
  constraint memberships_organization_id_id_key unique (organization_id, id)
);
create index memberships_user_id_idx on app.memberships (user_id) where revoked_at is null;

-- A shop shell: the marketplace connection arrives in phase 9.
create table app.shops (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  name text not null check (length(btrim(name)) between 1 and 200),
  marketplace text not null check (marketplace in ('etsy')),
  status text not null default 'pending' check (status in ('pending', 'connected', 'disconnected')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint shops_organization_id_id_key unique (organization_id, id)
);
create index shops_organization_id_idx on app.shops (organization_id);

-- Shop grants for roles that do not see every shop of the organization.
create table app.shop_access (
  id uuid primary key,
  organization_id uuid not null,
  membership_id uuid not null,
  shop_id uuid not null,
  created_at timestamptz not null default now(),
  constraint shop_access_membership_id_shop_id_key unique (membership_id, shop_id),
  -- Composite keys: a grant can never join a membership and a shop of different organizations.
  constraint shop_access_membership_fkey foreign key (organization_id, membership_id)
    references app.memberships (organization_id, id) on delete cascade,
  constraint shop_access_shop_fkey foreign key (organization_id, shop_id)
    references app.shops (organization_id, id) on delete cascade
);
create index shop_access_shop_id_idx on app.shop_access (shop_id);

-- Append-only audit log (ER-DATA-09). The writer arrives in slice 2.5.
create table audit.audit_log (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id),
  shop_id uuid references app.shops (id),
  occurred_at timestamptz not null default now(),
  actor_kind audit.actor_kind not null,
  actor_id text not null check (length(btrim(actor_id)) between 1 and 200),
  action text not null check (length(btrim(action)) between 1 and 100),
  entity_type text not null check (length(btrim(entity_type)) between 1 and 100),
  entity_id text,
  before jsonb,
  after jsonb,
  request_id text,
  correlation_id text,
  policy_version text,
  outcome text not null default 'succeeded' check (outcome in ('succeeded', 'denied', 'failed'))
);
create index audit_log_organization_id_occurred_at_idx
  on audit.audit_log (organization_id, occurred_at desc);

-- No role, not even the table owner, may change or delete an audit record.
create function audit.refuse_change() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception 'audit.audit_log is append-only' using errcode = 'insufficient_privilege';
end;
$$;
create trigger audit_log_append_only
  before update or delete on audit.audit_log
  for each row execute function audit.refuse_change();
create trigger audit_log_no_truncate
  before truncate on audit.audit_log
  for each statement execute function audit.refuse_change();

-- ---------------------------------------------------------------------------------------------
-- Helpers for policies. Security definer, so policies can read memberships without recursion;
-- they live in `private`, which only the API role `authenticated` may call (never exposed).

create function private.user_organization_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.organization_id
  from app.memberships m
  where m.user_id = (select auth.uid()) and m.revoked_at is null
$$;

create function private.user_admin_organization_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.organization_id
  from app.memberships m
  where m.user_id = (select auth.uid()) and m.revoked_at is null and m.role in ('owner', 'admin')
$$;

create function private.user_owner_organization_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.organization_id
  from app.memberships m
  where m.user_id = (select auth.uid()) and m.revoked_at is null and m.role = 'owner'
$$;

-- Owners and Admins see every shop of their organization; other roles see granted shops.
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

-- The memberships of the current user (for policies that must not read memberships directly).
create function private.user_membership_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.id from app.memberships m where m.user_id = (select auth.uid())
$$;

-- People who share an active organization with the current user.
create function private.colleague_user_ids() returns setof uuid
language sql stable security definer set search_path = '' as $$
  select m.user_id
  from app.memberships m
  where m.revoked_at is null
    and m.organization_id in (
      select own.organization_id from app.memberships own
      where own.user_id = (select auth.uid()) and own.revoked_at is null
    )
$$;

-- The session was verified with a second factor (Supabase sets `aal` in the JWT).
create function private.is_aal2() returns boolean
language sql stable set search_path = '' as $$
  select coalesce((select auth.jwt() ->> 'aal') = 'aal2', false)
$$;

revoke all on all functions in schema private from public;
grant usage on schema private to authenticated;
grant execute on function
  private.user_organization_ids(),
  private.user_admin_organization_ids(),
  private.user_owner_organization_ids(),
  private.user_shop_ids(),
  private.user_membership_ids(),
  private.colleague_user_ids(),
  private.is_aal2()
to authenticated;

-- ---------------------------------------------------------------------------------------------
-- Grants. `authenticated` is the role user requests run as; RLS decides which rows.

grant usage on schema app to authenticated;
grant usage on schema audit to authenticated;

grant select, update on app.organizations to authenticated;
grant select, update on app.user_profiles to authenticated;
grant select, insert, update, delete on app.memberships to authenticated;
grant select, insert, update, delete on app.shops to authenticated;
grant select, insert, delete on app.shop_access to authenticated;
grant select, insert on audit.audit_log to authenticated;

-- ---------------------------------------------------------------------------------------------
-- Row-level security on every table.

alter table app.organizations enable row level security;
alter table app.user_profiles enable row level security;
alter table app.memberships enable row level security;
alter table app.shops enable row level security;
alter table app.shop_access enable row level security;
alter table audit.audit_log enable row level security;

-- organizations: members read; only Owners change the organization.
create policy organizations_select on app.organizations for select to authenticated
  using (id in (select private.user_organization_ids()));
create policy organizations_update on app.organizations for update to authenticated
  using (id in (select private.user_owner_organization_ids()))
  with check (id in (select private.user_owner_organization_ids()));

-- user_profiles: your own and your colleagues'; you edit only your own.
create policy user_profiles_select on app.user_profiles for select to authenticated
  using (
    user_id = (select auth.uid()) or user_id in (select private.colleague_user_ids())
  );
create policy user_profiles_update on app.user_profiles for update to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

-- memberships: members see their colleagues; Owners and Admins manage them.
create policy memberships_select on app.memberships for select to authenticated
  using (organization_id in (select private.user_organization_ids()));
create policy memberships_insert on app.memberships for insert to authenticated
  with check (organization_id in (select private.user_admin_organization_ids()));
create policy memberships_update on app.memberships for update to authenticated
  using (organization_id in (select private.user_admin_organization_ids()))
  with check (organization_id in (select private.user_admin_organization_ids()));
create policy memberships_delete on app.memberships for delete to authenticated
  using (organization_id in (select private.user_admin_organization_ids()));

-- shops: visible per shop scope; Owners and Admins manage them.
create policy shops_select on app.shops for select to authenticated
  using (id in (select private.user_shop_ids()));
create policy shops_insert on app.shops for insert to authenticated
  with check (organization_id in (select private.user_admin_organization_ids()));
create policy shops_update on app.shops for update to authenticated
  using (organization_id in (select private.user_admin_organization_ids()))
  with check (organization_id in (select private.user_admin_organization_ids()));
create policy shops_delete on app.shops for delete to authenticated
  using (organization_id in (select private.user_admin_organization_ids()));

-- shop_access: you see your own grants; Owners and Admins see and manage all of them.
create policy shop_access_select on app.shop_access for select to authenticated
  using (
    organization_id in (select private.user_admin_organization_ids())
    or membership_id in (select private.user_membership_ids())
  );
create policy shop_access_insert on app.shop_access for insert to authenticated
  with check (organization_id in (select private.user_admin_organization_ids()));
create policy shop_access_delete on app.shop_access for delete to authenticated
  using (organization_id in (select private.user_admin_organization_ids()));

-- audit_log: Owners and Admins read their organization's records; a member's request may
-- append a record for its own organization (the API writes them, slice 2.5); nobody edits them.
create policy audit_log_select on audit.audit_log for select to authenticated
  using (organization_id in (select private.user_admin_organization_ids()));
create policy audit_log_insert on audit.audit_log for insert to authenticated
  with check (organization_id in (select private.user_organization_ids()));

-- Restrictive: every write to an owner-administered table needs an MFA session (FR-IAM-02).
create policy organizations_updates_need_aal2 on app.organizations
  as restrictive for update to authenticated
  using ((select private.is_aal2())) with check ((select private.is_aal2()));
create policy memberships_inserts_need_aal2 on app.memberships
  as restrictive for insert to authenticated with check ((select private.is_aal2()));
create policy memberships_updates_need_aal2 on app.memberships
  as restrictive for update to authenticated
  using ((select private.is_aal2())) with check ((select private.is_aal2()));
create policy memberships_deletes_need_aal2 on app.memberships
  as restrictive for delete to authenticated using ((select private.is_aal2()));
create policy shops_inserts_need_aal2 on app.shops
  as restrictive for insert to authenticated with check ((select private.is_aal2()));
create policy shops_updates_need_aal2 on app.shops
  as restrictive for update to authenticated
  using ((select private.is_aal2())) with check ((select private.is_aal2()));
create policy shops_deletes_need_aal2 on app.shops
  as restrictive for delete to authenticated using ((select private.is_aal2()));
create policy shop_access_inserts_need_aal2 on app.shop_access
  as restrictive for insert to authenticated with check ((select private.is_aal2()));
create policy shop_access_deletes_need_aal2 on app.shop_access
  as restrictive for delete to authenticated using ((select private.is_aal2()));
