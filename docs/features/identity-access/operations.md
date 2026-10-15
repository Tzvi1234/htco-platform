# Identity and access — Operations

## Bootstrapping the owner

1. The owner's user exists in Supabase Auth. Until the web app has its set-password page (slice 3.5 part 3), the owner creates it in the Supabase dashboard (Authentication → Users → Add user → Create new user, with "Auto Confirm User"), typing their own password; nobody else ever sees it.
2. **Production:** run the **Admin bootstrap** workflow (`.github/workflows/admin-bootstrap.yml`, owner-only) with the email, organization name and display name. **Elsewhere:** with `HTCO_DATABASE_URL` pointing at the target database, run:

   ```bash
   uv run htco admin bootstrap --email owner@example.com --organization "HT Commerce" --display-name "Owner"
   ```

3. The owner then signs in at https://dash.ht-co.app and is sent to set up the authenticator app (AC-3.5.3).
4. The command prints one JSON line: `created`, `organization_id`, `membership_id`. Running it again prints `created: false` with the same IDs and changes nothing.
4. Exit 65 means the email has no signed-up user (sign up first) or an input is invalid; 78 means `HTCO_DATABASE_URL` is missing.
5. The audit log holds `organization.created` and `membership.created` records by `system:bootstrap`, with the run's correlation ID.

## Changing a permission

1. Change the matrix in `docs/architecture/PERMISSION_MATRIX.md` and `_MATRIX` in `packages/application/src/htco_application/access/policy.py` in the same pull request; the matrix test fails if they differ.
2. Any permission change needs the owner's approval ("Ask the owner first" in CLAUDE.md) and a security note in the pull request.
