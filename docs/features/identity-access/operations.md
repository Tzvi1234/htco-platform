# Identity and access — Operations

Nothing runs in production yet.

## Bootstrapping the owner

1. The owner signs up through Supabase Auth (invitation from the Supabase dashboard; sign-up is invitation-only) and enrolls TOTP.
2. With `HTCO_DATABASE_URL` pointing at the target database, run:

   ```bash
   uv run htco admin bootstrap --email owner@example.com --organization "HT Commerce" --display-name "Owner"
   ```

3. The command prints one JSON line: `created`, `organization_id`, `membership_id`. Running it again prints `created: false` with the same IDs and changes nothing.
4. Exit 65 means the email has no signed-up user (sign up first) or an input is invalid; 78 means `HTCO_DATABASE_URL` is missing.
5. The audit log holds `organization.created` and `membership.created` records by `system:bootstrap`, with the run's correlation ID.

## Changing a permission

1. Change the matrix in `docs/architecture/PERMISSION_MATRIX.md` and `_MATRIX` in `packages/application/src/htco_application/access/policy.py` in the same pull request; the matrix test fails if they differ.
2. Any permission change needs the owner's approval ("Ask the owner first" in CLAUDE.md) and a security note in the pull request.
