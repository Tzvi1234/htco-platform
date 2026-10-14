# Identity and access — Operations

Nothing runs in production yet.

## Local database

1. Start Docker Desktop, then `just db-start` (Postgres on port 54322, Studio on 54323). It prints the `HTCO_TEST_DATABASE_URL` to set for the database tests.
2. `just db-test` runs the pgTAP tests and the Python tests marked `database`; `just db-reset` rebuilds the database from the migrations; `just db-stop` stops the stack.
3. A new migration: `npx --yes supabase@2.118.0 migration new <name>`, write the SQL with its RLS, add pgTAP tests, run `just db-reset` and `just db-test`. Never edit a migration that has been applied anywhere.

## Migrating a deployed project

Only the owner, from GitHub Actions → "Migrate database" → Run workflow on `main`. Leave "apply" unticked first and read the dry run; run again with "apply" ticked. The run summary records the migration list. Needs the repository secrets `SUPABASE_ACCESS_TOKEN` and `SUPABASE_DB_PASSWORD`, and the variable `SUPABASE_PROJECT_REF`, in the `development` environment. A failed migration is fixed by a new forward migration, never by editing the applied one; DigitalOcean rollbacks restore code, not data (ER-OPS-05).

## Changing a permission

1. Change the matrix in `docs/architecture/PERMISSION_MATRIX.md` and `_MATRIX` in `packages/application/src/htco_application/access/policy.py` in the same pull request; the matrix test fails if they differ.
2. Any permission change needs the owner's approval ("Ask the owner first" in CLAUDE.md) and a security note in the pull request.
