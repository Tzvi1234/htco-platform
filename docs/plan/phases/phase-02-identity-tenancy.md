# Phase 2 — Identity, tenancy, actors and audit

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | `htco-platform` |
| **Size** | M |
| **Depends on** | [Phase 1](phase-01-walking-skeleton.md); the Supabase Pro development project ([Phase 0](phase-00-groundwork.md) O-13) |
| **Requirements** | FR-ADM-01, FR-AUD-01, FR-IAM-01, FR-IAM-02, FR-IAM-03, FR-IAM-04 |
| **Mandatory tests owned** ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) | None. The two-tenant RLS checks built here are reused by Test 4 (nothing leaks across shops, [Phases 6](phase-06-workforce-claude-runtime.md) and [7](phase-07-pilot-office.md)) and Test 13 ([Phase 4](phase-04-durable-core-order-intake.md)) |
| **Status** | In progress (policy engine ahead of the database slices) |

## Progress

- [ ] 2.1 Database workflow
- [ ] 2.2 Identity and access schema with RLS
- [ ] 2.3 Authentication in the API
- [ ] 2.4 Policy engine v0 and first endpoints
- [ ] 2.5 Audit writer and bootstrap
- [ ] 2.6 User and shop-access management (API)

Slice 2.4 started early with its pure part: policy engine v0 (`htco_application.access.policy`), which needs no database. Its endpoints (`/v1/me`, `/v1/shops`) follow slices 2.1 to 2.3.

The living-office foundation of [ADR-0012](../../adr/0012-living-office-organization.md) is also in place as tested domain code with no database: `htco_domain.actors` (`ActorKind`, `ActorRef`), `htco_domain.workforce`, `htco_domain.tasks`, `htco_domain.automation`, `htco_domain.activity` and `htco_domain.improvement` (tests in `tests/domain/` and `tests/application/`). This phase uses `ActorRef` for the actor of every audit record. The employee registry tables (departments, employees, profile versions, tools, automations) arrive in [Phase 6](phase-06-workforce-claude-runtime.md); the activity record store in [Phase 5](phase-05-tasks-messages-approvals.md).

## Goal

The security spine of the system: organizations, users, roles and per-shop access; row-level security proven by tests with two tenants; JWT verification and MFA rules in the API; an append-only audit log. After this phase, every later table and endpoint inherits tenant isolation instead of reinventing it.

## Why now

Tenant isolation and audit cannot be bolted on later without rewriting every query. They must exist before the first business data: the order tables of [Phase 4](phase-04-durable-core-order-intake.md) (recorded fixtures) and the real shop of [Phase 9](phase-09-etsy-connection.md).

## Scope

**In:** Supabase project and local development flow, migrations pipeline, identity and access tables, RLS with helpers in a private schema, pgTAP tests, JWT verification against JWKS, `AuthContext`, policy engine v0 (role × capability), `GET /v1/me`, `GET /v1/shops`, audit writer with actor kinds (`user`, `employee`, `automation`, `advisor`, `system`), owner bootstrap command, `/readyz` database check.

**Out:** web sign-in screens ([Phase 3](phase-03-design-system-web-shell.md)), Etsy connection ([Phase 9](phase-09-etsy-connection.md)), approvals ([Phase 5](phase-05-tasks-messages-approvals.md)), the production Supabase project (only after the RTO and RPO are decided and the restore drill of [Phase 4](phase-04-durable-core-order-intake.md) passes).

## Owner prerequisites

- Supabase Pro **development** project created in the chosen region ([Phase 0](phase-00-groundwork.md) O-13; no production project yet) with the IPv4 add-on; asymmetric JWT signing keys enabled; legacy keys disabled; custom SMTP configured for Auth emails (Resend's SMTP or another provider); leaked-password protection on; TOTP MFA on.
- Owner's own user account created through Supabase Auth with MFA enrolled.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`.
2. Read this file, [PLAN.md](../PLAN.md) §1 and §6, [ADR-0005](../../adr/0005-hosting-digitalocean-supabase.md), [ADR-0011](../../adr/0011-data-access-and-migrations.md), [ADR-0012](../../adr/0012-living-office-organization.md), [DATA_MODEL](../../architecture/DATA_MODEL.md) (identity, audit, RLS pattern), [PERMISSION_MATRIX](../../architecture/PERMISSION_MATRIX.md), [THREAT_MODEL](../../architecture/THREAT_MODEL.md) T1, T14, T15, and `docs/features/identity-access/`.
3. The pure part of slice 2.4 (policy engine v0) is done; keep its tests passing. When the development project exists, build in order: 2.1, 2.2, 2.3, 2.4 (endpoints), 2.5, 2.6. One pull request per slice; acceptance criteria first as failing pgTAP or pytest tests marked with their AC ID.
4. Every table gets RLS in the same migration that creates it; tenant context is set with `set local` in every transaction; missing context fails closed.
5. Never in this phase: use the `service_role` or secret key in the request path; trust `user_metadata` for authorization; create a production Supabase project; edit a migration that has been applied; create tables that belong to later phases (orders, tasks, registry, agent runs).
6. Stop and ask the owner when: a Supabase setting in the prerequisites is missing or behaves differently from its documentation; the RLS design would need a security-definer function outside `private`; connection limits or pool sizes do not fit the plan (assumption A-13).
7. Finish with the feature-verification run against the deployed API with the owner's real account, the feature docs and the PLAN status table.

## Slices

### 2.1 — Database workflow

`supabase/` directory with config, migrations and tests; `just db-start`, `just db-reset`, `just db-test`, `just seed` (two organizations, their shops, one user per role); CI job running `supabase db start` and `supabase test db`; basejump test helpers installed only in the test database; per-component database roles and pool sizes documented; `/readyz` checks the database with a one-second timeout.

| ID | Acceptance criterion |
|---|---|
| AC-2.1.1 | A fresh clone can start the local stack and apply all migrations with one command. |
| AC-2.1.2 | A failing pgTAP test fails CI. |
| AC-2.1.3 | `/readyz` returns 503 when the database is unreachable and 200 when it is back, without restarting the app. |
| AC-2.1.4 | The production migration workflow runs only when the owner starts it, shows a dry-run first, and records the applied version. |

### 2.2 — Identity and access schema with RLS

Tables `organizations`, `user_profiles`, `memberships`, `shops` (shell: name, marketplace, status), `shop_access`; `audit.audit_log` with insert-only grants; helper functions in `private`; RLS on every table; restrictive `aal2` policies on owner-administered tables.

| ID | Acceptance criterion |
|---|---|
| AC-2.2.1 | An inventory test fails if any table in `app` or `audit` lacks RLS. |
| AC-2.2.2 | With two organizations seeded, a user of organization A selects zero rows of organization B in every table. |
| AC-2.2.3 | A Support user with access to shop 1 only cannot read shop 2's rows. |
| AC-2.2.4 | The application role cannot update or delete `audit.audit_log` rows. |
| AC-2.2.5 | An owner-only table rejects writes from an `aal1` session. |

### 2.3 — Authentication in the API

JWT verification against the project JWKS (ES256), checking `iss`, `aud`, `exp`, `sub`, `role`; `AuthContext` built from the token and **database memberships** (never `user_metadata`); user-scoped transactions with `set local role authenticated` and claims; 401 and 403 problem responses.

| ID | Acceptance criterion |
|---|---|
| AC-2.3.1 | Requests without a token, with an expired token, a wrong audience, a wrong issuer or a bad signature return 401 with distinct problem `type` values. |
| AC-2.3.2 | A token whose user has no membership returns 403. |
| AC-2.3.3 | A deliberately unfiltered repository query, run inside a user-scoped transaction, still returns only that user's tenant rows (RLS as defense in depth). |
| AC-2.3.4 | JWKS is cached with a bounded lifetime and refreshed on an unknown key ID. |

### 2.4 — Policy engine v0 and first endpoints

Role × capability table from the [permission matrix](../../architecture/PERMISSION_MATRIX.md) in `htco_application`; `GET /v1/me` (user, memberships, roles, MFA level) and `GET /v1/shops` (cursor-paginated, only granted shops).

| ID | Acceptance criterion |
|---|---|
| AC-2.4.1 | For every role in the matrix, a parametrized test asserts allowed and denied capabilities. |
| AC-2.4.2 | `GET /v1/shops` never returns a shop the user is not granted, including when a `shop_id` filter names another tenant's shop (404, not 403). |
| AC-2.4.3 | Pagination returns stable, non-overlapping pages and rejects limits above the maximum. |

### 2.5 — Audit writer and bootstrap

`AuditPort` and Postgres adapter; audit records for membership and role changes; `htco admin bootstrap` command that creates the owner's organization and membership idempotently. The audit actor is an `ActorRef` (`htco_domain.actors`): a kind (`user`, `employee`, `automation`, `advisor`, `system`) and an ID, stored as two columns, so records written later by employees, automations and the Decision Advisor need no schema change. In this phase only `user` and `system` actors write records.

| ID | Acceptance criterion |
|---|---|
| AC-2.5.1 | Running bootstrap twice creates one organization and one membership. |
| AC-2.5.2 | Every membership change writes one audit record with actor, organization, entity, before and after, and correlation ID. |
| AC-2.5.3 | The audit table accepts exactly the five actor kinds of `ActorKind` (a check constraint, tested in pgTAP); a record with an unknown kind or a blank actor ID is rejected; the bootstrap command writes its records as a `system` actor. |

### 2.6 — User and shop-access management (API)

Invite a user by email (Supabase invite sent through the custom SMTP), assign or change a role, grant or revoke shop access, remove a member. Owner and Admin only, with an `aal2` session; every change audited. Screens arrive in slice 3.9.

| ID | Acceptance criterion |
|---|---|
| AC-2.6.1 | Only Owner and Admin with an `aal2` session can invite, change roles or grant shop access; every other role gets 403. |
| AC-2.6.2 | An Admin cannot grant the Owner role or remove the Owner. |
| AC-2.6.3 | Revoking a membership or a shop grant takes effect on the user's next request, even with an unexpired token (authorization reads memberships from the database). |
| AC-2.6.4 | Each change writes one audit record with actor, target, before and after. |

## Test plan

pgTAP for RLS, grants and constraints; repository integration tests against the local stack; API tests with tokens issued by the local Supabase Auth; the two-tenant matrix over every route; feature-verification run against the deployed API with the owner's real account.

## Security checklist

- [ ] ASVS V8 (authorization) 8.2.2 and 8.4.1 covered by tests; V9 token validation; V6 MFA for owners.
- [ ] Request path never uses the Supabase secret or `service_role` key; request role lacks `BYPASSRLS`.
- [ ] Tenant context set with `set local` in every transaction; missing context fails closed.
- [ ] JWT verification rejects `alg: none` and HS256 when asymmetric keys are configured.

## Documentation

`docs/features/identity-access/`; DATA_MODEL tables marked as created, with the audit actor columns; PERMISSION_MATRIX confirmed; THREAT_MODEL T1, T14, T15 controls marked implemented.

## Demo

Show `GET /v1/me` for the owner; invite a Support user and grant one shop; show that user and a second test tenant failing to read data they were not granted, through the API and directly in SQL; show the audit records.

## Exit gate

- [ ] All AC-2.x pass; the development database (`htco-dev`) migrated through the owner-started workflow. The production project is created and migrated only after the [Phase 4](phase-04-durable-core-order-intake.md) restore drill and the owner's RTO and RPO decision.
- [ ] Connection from DigitalOcean to Supabase verified over IPv4 with the planned pool sizes.

## Rollback

Migrations are additive; the API version can be rolled back independently because the previous version ignores the new tables.

## Risks

| Risk | Mitigation |
|---|---|
| RLS policies slow queries | Indexes on tenant columns; `(select …)` wrapping; query plans checked in tests |
| Claims in tokens go stale after membership changes | Authorization reads memberships from the database on each request |

## Cost impact

Supabase Pro $25 plus IPv4 add-on $4 per month from this phase.
