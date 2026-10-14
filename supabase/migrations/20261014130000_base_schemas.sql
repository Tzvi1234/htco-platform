-- Base schemas (SOFTWARE_DESIGN §6, DATA_MODEL §1).
--   app      business tables; reached only through the HTCO API, never the Data API
--   private  security-definer helpers; no client role may even see it
--   audit    append-only audit log
-- Tables, grants and row-level security for each schema arrive with their slices.

create schema if not exists app;
create schema if not exists private;
create schema if not exists audit;

comment on schema app is 'HTCO business tables. Tenant-scoped; RLS on every table.';
comment on schema private is 'Security-definer helpers for RLS. Never exposed.';
comment on schema audit is 'Append-only audit log.';

-- Nothing is granted by default: each slice grants exactly what its roles need.
revoke all on schema app, private, audit from public;
revoke all on schema app, private, audit from anon, authenticated;

-- Signed-in users reach `app` and `audit` objects only through the grants and policies that later
-- migrations add; the schemas themselves must be visible for those policies to apply.
grant usage on schema app, audit to authenticated;
grant usage on schema app, private, audit to service_role;
