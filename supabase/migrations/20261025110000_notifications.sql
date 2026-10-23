-- Notifications and email (slice 6.4, FR-NTF-01, FR-EML-01).
--
-- `app.notifications`: one row per recipient and deduplication key (an incident, an alert, a
-- security event), with its kind and parameters (identifiers only). The recipient reads and marks
-- their own notifications; nobody else sees them.
-- `app.notification_settings` (the reader's language) and `app.notification_preferences` (in-app
-- and email per kind) belong to their user. No row: Hebrew, and the kind's defaults.
-- `app.email_deliveries`: one row per sent, refused or skipped email (status, provider ID, a
-- status code), never the address or the body. Owners and admins read their organization's rows
-- (the admin view of AC-6.4.3).
--
-- The worker reaches members only through two security-definer functions, inside its job's
-- organization: the recipients of a kind (user IDs and language) and one member's sign-in address
-- at send time. It reads no other identity table.
--
-- Safe on the live database: new tables, functions, grants and policies.

create table app.notifications (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null constraint notifications_kind_check check (kind ~ '^[a-z_]{1,40}$'),
  -- Identifiers only (htco_domain.notifications.check_params), at most 1 kB.
  params jsonb not null default '{}'::jsonb constraint notifications_params_check check (
    jsonb_typeof(params) = 'object' and pg_column_size(params) <= 1024
  ),
  dedup_key text not null
    constraint notifications_dedup_key_check check (char_length(dedup_key) between 1 and 120),
  shop_id uuid,
  incident_id uuid references app.incidents (id) on delete set null,
  created_at timestamptz not null,
  read_at timestamptz,
  constraint notifications_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint notifications_recipient_key unique (organization_id, user_id, dedup_key)
);
create index notifications_user_created_idx
  on app.notifications (user_id, organization_id, created_at desc);
create index notifications_unread_idx on app.notifications (user_id) where read_at is null;

create table app.notification_settings (
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  locale text not null default 'he' constraint notification_settings_locale_check check (locale in ('he', 'en')),
  updated_at timestamptz not null default now(),
  primary key (organization_id, user_id)
);

create table app.notification_preferences (
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null constraint notification_preferences_kind_check check (kind ~ '^[a-z_]{1,40}$'),
  in_app boolean not null default true,
  email boolean not null default true,
  updated_at timestamptz not null default now(),
  primary key (organization_id, user_id, kind)
);

create table app.email_deliveries (
  id uuid primary key,
  outbox_id uuid not null,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null,
  kind text not null constraint email_deliveries_kind_check check (kind ~ '^[a-z_]{1,40}$'),
  status text not null constraint email_deliveries_status_check check (status in ('sent', 'failed', 'skipped')),
  provider_id text constraint email_deliveries_provider_id_check check (char_length(provider_id) <= 120),
  -- A status code or a type name, never the address or the provider's message.
  error text constraint email_deliveries_error_check check (error ~ '^[A-Za-z0-9_.: -]{1,64}$'),
  created_at timestamptz not null,
  constraint email_deliveries_outbox_key unique (outbox_id)
);
create index email_deliveries_organization_idx
  on app.email_deliveries (organization_id, created_at desc);

alter table app.notifications enable row level security;
alter table app.notification_settings enable row level security;
alter table app.notification_preferences enable row level security;
alter table app.email_deliveries enable row level security;
revoke all on app.notifications, app.notification_settings, app.notification_preferences,
  app.email_deliveries from public, anon, authenticated, service_role;

-- Members: their own notifications (read, and mark read); their own settings and preferences.
grant select on app.notifications to authenticated;
grant update (read_at) on app.notifications to authenticated;
create policy notifications_own_select on app.notifications for select to authenticated
  using (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())));
create policy notifications_own_update on app.notifications for update to authenticated
  using (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())))
  with check (user_id = (select auth.uid()));

grant select, insert, update (locale, updated_at) on app.notification_settings to authenticated;
create policy notification_settings_own on app.notification_settings for all to authenticated
  using (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())))
  with check (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())));

grant select, insert, update (in_app, email, updated_at) on app.notification_preferences to authenticated;
create policy notification_preferences_own on app.notification_preferences for all to authenticated
  using (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())))
  with check (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())));

grant select on app.email_deliveries to authenticated;
create policy email_deliveries_admin_select on app.email_deliveries for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));

-- The worker ------------------------------------------------------------------------------------

grant select, insert on app.notifications to htco_worker;
create policy notifications_worker on app.notifications for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));
grant select on app.notification_settings, app.notification_preferences to htco_worker;
create policy notification_settings_worker on app.notification_settings for select to htco_worker
  using (organization_id = (select private.worker_organization_id()));
create policy notification_preferences_worker on app.notification_preferences for select
  to htco_worker using (organization_id = (select private.worker_organization_id()));
grant select, insert on app.email_deliveries to htco_worker;
create policy email_deliveries_worker on app.email_deliveries for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

-- The active members of the worker's organization with one of `p_roles` (or the one user), with
-- their language. Owners and admins reach every shop; other roles only with access to `p_shop`.
create function private.notification_recipients(p_roles text[], p_user uuid, p_shop uuid)
returns table (user_id uuid, locale text)
language sql stable security definer set search_path = '' as $$
  select m.user_id, coalesce(s.locale, 'he')
  from app.memberships m
  left join app.notification_settings s
    on s.organization_id = m.organization_id and s.user_id = m.user_id
  where m.organization_id = (select private.worker_organization_id())
    and m.revoked_at is null
    and (p_user is null or m.user_id = p_user)
    and (p_roles is null or m.role = any (p_roles))
    and (
      m.role in ('owner', 'admin') or p_shop is null
      or exists (select 1 from app.shop_access a where a.membership_id = m.id and a.shop_id = p_shop)
    )
$$;
revoke all on function private.notification_recipients(text[], uuid, uuid) from public;
grant execute on function private.notification_recipients(text[], uuid, uuid) to htco_worker;

-- One active member's sign-in address, at send time; null when they left the organization.
create function private.member_email(p_user uuid) returns text
language sql stable security definer set search_path = '' as $$
  select u.email::text
  from auth.users u
  join app.memberships m on m.user_id = u.id
  where u.id = p_user and m.revoked_at is null
    and m.organization_id = (select private.worker_organization_id())
  limit 1
$$;
revoke all on function private.member_email(uuid) from public;
grant execute on function private.member_email(uuid) to htco_worker;
