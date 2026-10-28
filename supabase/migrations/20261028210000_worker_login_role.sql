-- The worker's own database login (architecture audit 5.1, ER-SEC-08).
--
-- Until now the worker logged in with the API's login, the owner of the tables that reads the
-- sealed vault columns, and only switched down with `set role htco_worker`: a `reset role`, a
-- stray `set role` or an injected statement would regain every right on every tenant.
--
-- `htco_worker_login` is the worker's login. It holds no right of its own (NOINHERIT, no
-- superuser, no BYPASSRLS, no member of the owner) and can only switch to `htco_worker`, whose
-- grants and policies are those of the job-queue migrations. So it reaches exactly what the
-- worker role reaches, and a `reset role` falls back to a role that reaches nothing.
--
-- No password here: the repository is public. The owner sets it once in Supabase with
-- `alter role htco_worker_login password '...'` (docs/features/job-queue/operations.md); until
-- then the role cannot log in and the worker keeps its fallback on the API's login, with a
-- WARNING at every start.

do $$
begin
  if not exists (select from pg_catalog.pg_roles where rolname = 'htco_worker_login') then
    create role htco_worker_login
      login noinherit nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
  end if;
end
$$;
comment on role htco_worker_login is
  'The worker''s login (architecture audit 5.1): no rights of its own, switches to htco_worker.';

-- Only the switch to the worker role: no inherited rights, so nothing before `set role`.
grant htco_worker to htco_worker_login with inherit false, set true;
-- The migration owner may act as the login (pgTAP), as with the other worker roles.
grant htco_worker_login to postgres with inherit false;
