-- A signed-in user appends audit records as themselves only (architecture audit 3.1, ER-DATA-06,
-- ER-DATA-09).
--
-- The insert policy of slice 2.2 checked only the organization, so a session reaching the
-- database as `authenticated` could write `actor_kind = 'system'` or another user's ID into the
-- append-only log, where it can never be corrected. Now the record's actor must be the signed-in
-- user. The API's user-scoped writes (members and invitations, slice 3.x) already record exactly
-- that; records of the system, automations and employees come from the worker roles and the API's
-- login (which RLS does not restrict), and `service_role` bypasses RLS as before.

drop policy audit_log_insert on audit.audit_log;
create policy audit_log_insert on audit.audit_log for insert to authenticated
  with check (
    organization_id = any (array(select private.user_org_ids()))
    and actor_kind = 'user'
    and actor_id = (select auth.uid())::text
  );
