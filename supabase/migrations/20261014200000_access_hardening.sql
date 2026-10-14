-- Hardening after the review of slices 2.2 to 2.6.

-- Memberships are revoked (`revoked_at`), never deleted: deleting would erase the history that
-- revocation keeps, and cascade away the shop grants. The API only revokes.
drop policy memberships_delete on app.memberships;
drop policy memberships_delete_aal2 on app.memberships;
revoke delete on app.memberships from authenticated;

-- Shop pages filter by organization and follow the shop ID (`/v1/shops`).
drop index app.shops_organization_id_idx;
create index shops_organization_id_id_idx on app.shops (organization_id, id);

-- The user ID behind an email that already has an account, for an Owner or Admin of the
-- organization in an `aal2` session; null otherwise. Lets an invitation finish when the sign-in
-- service already knows the email (an earlier, interrupted invitation, or a member of another
-- organization). Security definer, so callers never read `auth.users` themselves.
create function app.registered_user_id(p_organization_id uuid, p_email text) returns uuid
language sql stable security definer set search_path = '' as $$
  select u.id
  from auth.users u
  where lower(u.email) = lower(btrim(p_email))
    and p_organization_id = any (array(select private.user_admin_org_ids()))
    and (select private.session_is_aal2())
  limit 1
$$;
revoke all on function app.registered_user_id(uuid, text) from public, anon;
grant execute on function app.registered_user_id(uuid, text) to authenticated;
