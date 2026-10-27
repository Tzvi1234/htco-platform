-- Slice 7.6: notification preferences per severity and per shop (FR-NTF-01, AC-7.6.1).
--
-- `app.notification_severity_preferences` (in-app and email per severity) and
-- `app.notification_shop_mutes` (shops whose non-critical notifications a user does not want)
-- belong to their user, next to the per-kind `app.notification_preferences` of slice 6.4. No row:
-- the defaults (everything on; a shop not muted). Security notifications are never muted, and a
-- critical notification always reaches the inbox: `htco_domain.notifications.delivery` decides,
-- and no row can change that. A user may mute only a shop they can see.

create table app.notification_severity_preferences (
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  severity text not null constraint notification_severity_preferences_severity_check check (
    severity in ('low', 'medium', 'high', 'critical')
  ),
  in_app boolean not null default true,
  email boolean not null default true,
  updated_at timestamptz not null default now(),
  primary key (organization_id, user_id, severity)
);

create table app.notification_shop_mutes (
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  shop_id uuid not null,
  created_at timestamptz not null default now(),
  primary key (organization_id, user_id, shop_id),
  constraint notification_shop_mutes_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade
);

alter table app.notification_severity_preferences enable row level security;
alter table app.notification_shop_mutes enable row level security;
revoke all on app.notification_severity_preferences, app.notification_shop_mutes
  from public, anon, authenticated, service_role;

-- Members: their own rows; a mute only for a shop of theirs.
grant select, insert, delete on app.notification_severity_preferences to authenticated;
grant update (in_app, email, updated_at) on app.notification_severity_preferences to authenticated;
create policy notification_severity_preferences_own on app.notification_severity_preferences
  for all to authenticated
  using (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())))
  with check (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())));

grant select, insert, delete on app.notification_shop_mutes to authenticated;
create policy notification_shop_mutes_own on app.notification_shop_mutes for all to authenticated
  using (user_id = (select auth.uid()) and organization_id = any (array(select private.user_org_ids())))
  with check (
    user_id = (select auth.uid())
    and organization_id = any (array(select private.user_org_ids()))
    and shop_id = any (array(select private.user_shop_ids()))
  );

-- The worker reads them, in its job's organization, to decide each recipient's channels.
grant select on app.notification_severity_preferences, app.notification_shop_mutes to htco_worker;
create policy notification_severity_preferences_worker on app.notification_severity_preferences
  for select to htco_worker using (organization_id = (select private.worker_organization_id()));
create policy notification_shop_mutes_worker on app.notification_shop_mutes
  for select to htco_worker using (organization_id = (select private.worker_organization_id()));
