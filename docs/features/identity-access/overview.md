# Identity and access — Overview

| | |
|---|---|
| **Slug** | `identity-access` |
| **Phase / slices** | Phase 2 — slices 2.1 to 2.6 |
| **Requirements** | [FR-IAM-01 to FR-IAM-04, FR-ADM-01, FR-AUD-01](../../product/PRD.md) |
| **Status** | In progress (slice 2.4 part 1 merged; slice 2.1 database workflow) |
| **Last updated** | 2026-10-14 |

## What and why

The security spine: who belongs to which organization, with which role, on which shops, and how strongly they signed in. Slice 2.4's first part turns the [permission matrix](../../architecture/PERMISSION_MATRIX.md) into code: one function decides every human action, deny by default, and a test keeps it identical to the documented table.

## Acceptance criteria

| ID | Criterion | Slice | Status |
|---|---|---|---|
| AC-2.1.1 | A fresh clone can start the local stack and apply all migrations with one command. | 2.1 | Pass in CI (`supabase db start`); locally `just db-start` needs Docker |
| AC-2.1.2 | A failing pgTAP test fails CI. | 2.1 | Pass |
| AC-2.1.3 | `/readyz` returns 503 when the database is unreachable and 200 when it is back, without restarting the app. | 2.1 | Pass |
| AC-2.1.4 | The production migration workflow runs only when the owner starts it, shows a dry-run first, and records the applied version. | 2.1 | Built; verified against a real project when it exists |
| AC-2.4.1 | For every role in the matrix, a parametrized test asserts allowed and denied capabilities. | 2.4 | Pass |
| AC-2.4.2 | `GET /v1/shops` never returns a shop the user is not granted, including when a `shop_id` filter names another tenant's shop (404, not 403). | 2.4 (part 2, needs 2.1 to 2.3) | Open |
| AC-2.4.3 | Pagination returns stable, non-overlapping pages and rejects limits above the maximum. | 2.4 (part 2) | Open |

The criteria of slices 2.1, 2.2, 2.3, 2.5 and 2.6 join here when they start.

## Scope

- **In (2.1):** `supabase/` (config with invitation-only sign-up, 12-character passwords, TOTP MFA; base schemas `app`, `private`, `audit`), `htco_persistence` with the `/readyz` database check, `HTCO_DATABASE_URL` (required when deployed), the `database` CI job, the owner-started `migrate.yml` workflow.
- **In (2.4, part 1):** `Role` and `AssuranceLevel` (domain), `Capability`, `Grant`, `Principal`, `Decision` and `decide` (application), the matrix as data.
- **Out:** building the `Principal` from a verified token and database memberships (2.3), the endpoints (2.4 part 2), RLS (2.2), approvals (Phase 7), worker and agent permissions (Phases 5 and 8).

## Known limits

- The two-person rule for high-risk approvals is a later policy option (the matrix note): today high-risk approvals rely on the owner's step-up.
