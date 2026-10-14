-- HTCO schemas (SOFTWARE_DESIGN §6, DATA_MODEL). Business tables live in `app`; security-definer
-- helpers in `private`; the append-only audit log in `audit`. None of them is exposed through
-- the Supabase Data API: clients reach data only through the HTCO API (config.toml [api]).
-- Tables, grants and RLS policies arrive with the slices that need them (phase 2, slice 2.2 on).

create schema app;
create schema private;
create schema audit;

comment on schema app is 'HTCO business tables. Every table has RLS (phase 2, slice 2.2).';
comment on schema private is 'HTCO security-definer helpers. Never granted to API clients.';
comment on schema audit is 'HTCO append-only audit log. Insert-only for application roles.';

-- Deny by default (ER-SEC-02): no role but the owner can use these schemas until a migration
-- grants exactly what a slice needs.
revoke all on schema app, private, audit from public;
revoke all on schema app, private, audit from anon, authenticated, service_role;

-- Objects created later start without privileges for the API roles, too.
alter default privileges in schema app, private, audit revoke all on tables from public;
alter default privileges in schema app, private, audit revoke all on functions from public;
alter default privileges in schema app, private, audit revoke all on sequences from public;
