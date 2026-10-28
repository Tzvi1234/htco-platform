-- The failed-email admin view (AC-6.4.3, `GET /v1/operations/email-failures`).
--
-- `app.email_deliveries` gains what the delivery was about, so the Owner and Admins can follow a
-- failure to its order or incident: the shop, the incident and the Etsy receipt the notification
-- named (identifiers only, all nullable: a security notice names none, and rows written before
-- this migration keep nulls). The worker already inserts every column of the table.
--
-- `app.admin_member_email_masked`: the recipient's sign-in address, masked (`o***@e***.test`),
-- for an Owner or Admin of the organization the delivery belongs to; null for anyone else. The
-- full address never leaves the database: the table keeps none, and this is the only way the API
-- reaches one for the view.
--
-- Safe on the live database: nullable columns, an index, one function.

alter table app.email_deliveries
  add column shop_id uuid,
  -- No foreign key: the email is already sent when its row is written, and an incident removed
  -- meanwhile (with its shop) must not make the record fail.
  add column incident_id uuid,
  add column receipt_id bigint constraint email_deliveries_receipt_id_check check (receipt_id > 0);

create index email_deliveries_failed_idx
  on app.email_deliveries (organization_id, id desc) where status <> 'sent';

create function app.admin_member_email_masked(p_organization uuid, p_user uuid) returns text
language sql stable security definer set search_path = '' as $$
  select
    left(split_part(u.email::text, '@', 1), 1) || '***@'
    || left(split_part(u.email::text, '@', 2), 1) || '***'
    || coalesce(substring(split_part(u.email::text, '@', 2) from '(\.[^.]+)$'), '')
  from auth.users u
  where u.id = p_user
    and p_organization = any (array(select private.user_admin_org_ids()))
    -- A member of that organization, or one who was (the email may have failed before they left).
    and exists (
      select 1 from app.memberships m
      where m.user_id = p_user and m.organization_id = p_organization
    )
$$;
revoke all on function app.admin_member_email_masked(uuid, uuid)
  from public, anon, authenticated, service_role;
grant execute on function app.admin_member_email_masked(uuid, uuid) to authenticated;
