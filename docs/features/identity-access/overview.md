# Identity and access — Overview

| | |
|---|---|
| **Slug** | `identity-access` |
| **Phase / slices** | Phase 2 — slices 2.1 to 2.6 |
| **Requirements** | [FR-IAM-01 to FR-IAM-04, FR-ADM-01, FR-AUD-01](../../product/PRD.md) |
| **Status** | In progress (slices 2.1, 2.2 and 2.4 part 1 merged; slice 2.3 authentication in the API) |
| **Last updated** | 2026-10-15 |

## What and why

The security spine: who belongs to which organization, with which role, on which shops, and how strongly they signed in. Slice 2.4's first part turns the [permission matrix](../../architecture/PERMISSION_MATRIX.md) into code: one function decides every human action, deny by default, and a test keeps it identical to the documented table.

## Acceptance criteria

| ID | Criterion | Slice | Status |
|---|---|---|---|
| AC-2.1.1 | A fresh clone can start the local stack and apply all migrations with one command. | 2.1 | Pass in CI (`supabase db start`); locally `just db-start` needs Docker |
| AC-2.1.2 | A failing pgTAP test fails CI. | 2.1 | Pass |
| AC-2.1.3 | `/readyz` returns 503 when the database is unreachable and 200 when it is back, without restarting the app. | 2.1 | Pass |
| AC-2.1.4 | The production migration workflow runs only when the owner starts it, shows a dry-run first, and records the applied version. | 2.1 | Pass: dry run, then the owner's `apply: true` run on 2026-10-14 applied both migrations to project `htco` |
| AC-2.2.1 | An inventory test fails if any table in `app` or `audit` lacks RLS. | 2.2 | Pass |
| AC-2.2.2 | With two organizations seeded, a user of organization A selects zero rows of organization B in every table. | 2.2 | Pass |
| AC-2.2.3 | A Support user with access to shop 1 only cannot read shop 2's rows. | 2.2 | Pass |
| AC-2.2.4 | The application role cannot update or delete `audit.audit_log` rows. | 2.2 | Pass (`authenticated`, `service_role`, and the table owner through a trigger) |
| AC-2.2.5 | An owner-only table rejects writes from an `aal1` session. | 2.2 | Pass |
| AC-2.3.1 | Requests without a token, with an expired token, a wrong audience, a wrong issuer or a bad signature return 401 with distinct problem `type` values. | 2.3 | Pass |
| AC-2.3.2 | A token whose user has no membership returns 403. | 2.3 | Pass |
| AC-2.3.3 | A deliberately unfiltered repository query, run inside a user-scoped transaction, still returns only that user's tenant rows (RLS as defense in depth). | 2.3 | Pass (CI `database` job) |
| AC-2.3.4 | JWKS is cached with a bounded lifetime and refreshed on an unknown key ID. | 2.3 | Pass |
| AC-2.5.1 | Running bootstrap twice creates one organization and one membership. | 2.5 | Pass |
| AC-2.5.2 | Every membership change writes one audit record with actor, organization, entity, before and after, and correlation ID. | 2.5 | Pass (bootstrap and every 2.6 endpoint; the correlation ID is the request ID) |
| AC-2.5.3 | The audit table accepts exactly the five actor kinds of `ActorKind`; an unknown kind or a blank actor ID is rejected; the bootstrap writes as a `system` actor. | 2.5 | Pass |
| AC-2.6.1 | Only Owner and Admin with an `aal2` session can invite, change roles or grant shop access; every other role gets 403. | 2.6 | Pass |
| AC-2.6.2 | An Admin cannot grant the Owner role or remove the Owner. | 2.6 | Pass (nobody changes or removes the Owner's membership through the API) |
| AC-2.6.3 | Revoking a membership or a shop grant takes effect on the user's next request, even with an unexpired token. | 2.6 | Pass |
| AC-2.6.4 | Each change writes one audit record with actor, target, before and after. | 2.6 | Pass |
| AC-2.6.5 | An invitation returns a one-time invitation link once, uncached (`no-store`) and never logged; an email that already has an account is added without a link. | 2.6 | Pass |
| AC-2.4.1 | For every role in the matrix, a parametrized test asserts allowed and denied capabilities. | 2.4 | Pass |
| AC-2.4.2 | `GET /v1/shops` never returns a shop the user is not granted, including when a `shop_id` filter names another tenant's shop (404, not 403). | 2.4 (part 2) | Pass |
| AC-2.4.3 | Pagination returns stable, non-overlapping pages and rejects limits above the maximum. | 2.4 (part 2) | Pass |

The criteria of slices 2.1, 2.2, 2.3, 2.5 and 2.6 join here when they start.

## Scope

- **In (2.1):** `supabase/` (config with invitation-only sign-up, 12-character passwords, TOTP MFA; base schemas `app`, `private`, `audit`), `htco_persistence` with the `/readyz` database check, `HTCO_DATABASE_URL` (required when deployed), the `database` CI job, the owner-started `migrate.yml` workflow.
- **In (2.2):** tables `app.organizations`, `app.user_profiles`, `app.memberships` (seven roles, one active membership per user and organization), `app.shops` (shell), `app.shop_access` (composite keys keep a grant inside one organization), `audit.audit_log` (five actor kinds, append-only); RLS on every table with security-definer helpers in `private`; writes to owner-administered tables need Owner or Admin and an `aal2` session; `supabase/seed.sql` (`just seed`); a unit test keeps the SQL role and actor lists equal to the domain enums.
- **In (2.4, part 1):** `Role` and `AssuranceLevel` (domain), `Capability`, `Grant`, `Principal`, `Decision` and `decide` (application), the matrix as data.
- **In (2.3):** `htco_platform.jwt` (ES256 only, issuer, audience, expiry, `role = authenticated`, JWKS cache with refresh on an unknown key ID, fail-closed when the key set is unreachable); `htco_application.access.authentication` (`AuthContext` and `Principal` from **database** memberships, never token claims); `htco_persistence.database` (pool; user-scoped transactions with `set local role authenticated` and the verified claims) and `PostgresMembershipReader`; `htco_api.auth` (`CurrentAuth` dependency and eight problem types: `unauthenticated`, `token-expired`, `token-wrong-audience`, `token-wrong-issuer`, `token-bad-signature`, `token-invalid`, `no-membership`, `auth-unavailable`); `HTCO_SUPABASE_URL`.
- **In (2.6):** `/v1/organizations/{organization_id}/members` (list), `/invitations` (POST), `/members/{membership_id}` (PATCH role, DELETE), `/members/{membership_id}/shops/{shop_id}` (PUT grant, DELETE revoke); `htco_application.access.management` (use cases on `manage_access`, Owner-only Owner role, the Owner's membership untouchable, one audit record per change, idempotent grants); `PostgresAccessStore` (user-scoped transactions, so RLS and its `aal2` policies apply too; a write RLS filters out raises); `htco_platform.supabase_admin.SupabaseInviter` (Auth admin `generate_link`, type `invite`, with `HTCO_SUPABASE_SECRET_KEY`: returns a one-time invitation link instead of sending an email); `Allow` on 405 lists every method of the path.
- **In (2.5):** `htco_application.audit` (`AuditRecord` with an `ActorRef` actor, `AuditWriter`), `htco_application.bootstrap` (one unit of work: find the signed-up user, reuse an existing owner membership or create organization, profile and owner membership, two audit records by `system:bootstrap`), `htco_persistence.audit.append_audit` (inside the caller's transaction), `PostgresBootstrapStore` (one transaction behind an advisory lock), the `htco admin bootstrap` command.
- **In (2.4, part 2):** `GET /v1/me` (user, session strength, role per organization) and `GET /v1/shops` (filters `organization_id` and `shop_id`; opaque cursor; `limit` 1 to 100, default 50); `htco_application.shops.list_shops` (scope from the policy engine, carried to the repository as an explicit filter; a shop or organization the caller cannot see is 404); `PostgresShopReader` under the caller's RLS; the bearer scheme in the OpenAPI contract; a missing token is 401 even when sign-in is unavailable.
- **Out:** approvals (Phase 7), worker and agent permissions (Phases 5 and 8).

## Invitation links

There is no email provider (custom SMTP) yet, and Supabase's built-in email delivers only to the project team's addresses, so the API does not ask Supabase to send an invitation email. Instead:

1. An Owner or Admin with an `aal2` session calls `POST /v1/organizations/{organization_id}/invitations` with `{"email", "role"}`.
2. The API calls Supabase Auth's admin `generate_link` (`{"type": "invite", "email", "redirect_to": HTCO_INVITE_REDIRECT_URL}`), which creates the user (or refreshes a pending invitation) and returns a one-time `action_link`. The membership is created and audited as before.
3. The answer (201) is the member plus `invite_link`, with `Cache-Control: no-store`. The caller copies the link and sends it to the invitee over a channel they trust. When the email already had an account, the user is added and `invite_link` is `null` (they sign in as usual).
4. The invitee opens the link: Supabase verifies it and redirects to the web app's accept page (`https://dash.ht-co.app/accept-invite` in production) with the user's tokens in the URL fragment. The page asks for a password and posts it with the tokens to `POST /v1/auth/invitations/accept` ([auth-bff](../auth-bff/overview.md)); the invitee is then signed in.

The link is a bearer credential for the invitee's new account: it is shown once to an Owner or Admin who signed in with MFA, the response is `no-store`, it is never logged or audited (and hidden from `repr`), and it is not stored by HTCO, so it cannot be fetched again (inviting again issues a new link). It expires with Supabase's email OTP expiry (Auth settings) and works once. The redirect URL must be in Supabase Auth's redirect allow-list. When an email provider arrives, Supabase can send the same invitation by email instead.

## Passkeys as a second factor (issue #113)

Next to the authenticator app (TOTP), a user can add passkeys (Face ID, Touch ID, Windows Hello, Android biometrics) as Supabase Auth WebAuthn MFA factors, name each one and remove them; either factor makes the session `aal2`. Adding or removing a factor needs an `aal2` session, the last authenticator app always stays, and both changes are audited. It is built behind `HTCO_PASSKEYS_ENABLED` and is **off on the hosted project** until the owner enables Supabase's WebAuthn MFA add-on and sets the production RP (`ht-co.app`, `https://dash.ht-co.app`). Locally, `supabase/config.toml` enables it for RP ID `localhost`. Details, routes and the switch-on steps: [passkeys](passkeys.md).

[ADR-0021](../../adr/0021-authenticator-service.md) is a different thing: the Authenticator that holds the TOTP secrets of the **shops'** accounts (Etsy, partners) for the browser worker. HTCO's own sign-in factors stay in Supabase Auth.

## Known limits

- The two-person rule for high-risk approvals is a later policy option (the matrix note): today high-risk approvals rely on the owner's step-up.
