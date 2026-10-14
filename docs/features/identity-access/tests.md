# Identity and access — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Database (pgTAP) | Base schemas exist; `anon` and `authenticated` cannot use `private` or create in `app` and `audit` | `supabase/tests/database/00_schemas.test.sql` |
| Database (pgTAP) | Identity tables exist; RLS inventory; two-tenant isolation in every table; Support limited to its granted shop; append-only audit for `authenticated`, `service_role` and the owner; `aal2` required for writes to owner-administered tables; closed role and actor lists; cross-organization grants rejected; `anon` and direct helper calls refused (49 assertions) | `supabase/tests/database/01_identity_access.test.sql` |
| Persistence | The SQL check constraints list exactly the domain's `Role` and `ActorKind` values | `tests/persistence/test_schema_mirrors_domain.py` |
| Platform | Token verification: valid tokens, every rejection reason, `alg: none`, HS256 with a shared secret, tampered payload, a forged key with a known key ID; JWKS cache lifetime, refresh on rotation, cooldown against forged key IDs, fail-closed, a failed refresh keeps current keys, non-ES256 keys ignored, malformed documents | `tests/platform/test_jwt.py` |
| Application | `AuthContext` from database memberships; no membership refused; `Principal` per organization; default organization | `tests/application/test_authentication.py` |
| API | `CurrentAuth`: valid token; ten unauthenticated cases with their problem types and `WWW-Authenticate`; no membership 403; key set down 503; no authenticator 503; rejections logged without the token; resources start and stop with the app; settings and composition | `tests/api/test_auth.py`, `tests/api/test_settings.py`, `tests/api/test_main.py` |
| Integration (`-m db`) | An unfiltered query in a user-scoped transaction returns only the user's tenant rows; no subject sees nothing; role and claims do not leak to the next use of the connection; the membership reader for Support, Operations, an Owner and an unknown user | `tests/integration/test_user_scope.py` |
| Application | `list_shops`: organization-wide and granted scopes, shop filter (granted, not granted, other tenant, unknown), pages, limits, several organizations, non-member organization | `tests/application/test_shop_queries.py` |
| API | `/v1/me` and `/v1/shops` through the app: bodies, scopes, 404 cases, pagination, limit and cursor validation, `organization-required`, no shop store | `tests/api/test_me_and_shops.py` |
| Integration (`-m db`) | `PostgresShopReader` on the seed: ordering, explicit scope, paging, RLS hiding ungranted shops without the explicit scope, other tenant's shops never returned | `tests/integration/test_shop_reader.py` |
| Persistence | The database check fails fast on a closed port, is named `database`, hides its connection string | `tests/persistence/test_database_check.py` |
| API | Deployed environments refuse to start without `HTCO_DATABASE_URL`; with it, `/readyz` reports the database; without it locally, configuration only | `tests/api/test_main.py` |
| Integration (`-m db`) | `/readyz` against real Postgres through a TCP relay: 200, 503 when the relay closes, 200 again without a restart | `tests/integration/test_database_readiness.py` |
| Application | The documented matrix parsed from `PERMISSION_MATRIX.md` equals `PERMISSIONS`; every role × capability (140 cases) for a fully assured user; shop scope (granted, not granted, organization-wide, another organization's shop for every role × capability, lists); masked and approval grants; MFA; step-up window boundaries; check order; naive clocks | `tests/application/test_access_policy.py` |

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
| AC-2.1.1 | CI `database` job: `supabase db start` applies every migration |
| AC-2.1.2 | CI `database` job: `supabase test db` |
| AC-2.1.3 | `test_readyz__database_down_then_back__503_then_200_without_restart`, `test_main__database_url__readyz_checks_the_database` |
| AC-2.1.4 | `.github/workflows/migrate.yml` (`workflow_dispatch` only, dry run first, `migration list` in the summary) |
| AC-2.2.1 | `01_identity_access.test.sql`: "every table in app and audit has row-level security enabled" |
| AC-2.2.2 | `01_identity_access.test.sql`: owner A and owner B blocks (reads, writes into the other organization) |
| AC-2.2.3 | `01_identity_access.test.sql`: "Support A sees shop A1 and not shop A2" |
| AC-2.2.4 | `01_identity_access.test.sql`: AC-2.2.4 block |
| AC-2.2.5 | `01_identity_access.test.sql`: AC-2.2.5 block |
| AC-2.3.1 | `test_unauthenticated_requests__401_with_a_distinct_problem_type` (10 cases), `test_the_five_ac_cases__have_five_different_types` |
| AC-2.3.2 | `test_user_without_membership__403`, `test_authenticate__no_membership__is_refused` |
| AC-2.3.3 | `test_unfiltered_query__returns_only_the_users_tenant_rows` |
| AC-2.3.4 | `test_jwks__is_fetched_once_while_fresh`, `test_jwks__is_refetched_after_its_lifetime`, `test_jwks__unknown_kid__refreshes_and_accepts_a_rotated_key`, `test_jwks__unknown_kids_within_the_cooldown__do_not_refetch` |
| AC-2.4.1 | `test_permissions__match_the_documented_matrix` (20 cases), `test_decide__fully_assured_principal__gets_exactly_the_documented_answer` (140 cases) |
| AC-2.4.2 | `test_shops__filter_naming_a_shop_the_user_cannot_see__404` (not granted, other tenant, unknown), `test_shops__owner_naming_another_tenants_shop__404`, `test_shop_filter__a_shop_the_user_cannot_see__is_not_found`, `test_another_tenants_shop__is_never_returned` (integration) |
| AC-2.4.3 | `test_shops__pages_are_stable_and_do_not_overlap`, `test_shops__limit_outside_1_to_100__422`, `test_pages__are_stable_and_do_not_overlap`, `test_after_and_limit__page_through_the_shops` (integration) |

## Edge cases considered

- Step-up exactly at 10 minutes (expired), one second before (valid), never, and in the future (refused).
- An Owner naming a shop of another organization (denied, even with the shop in its granted set);
- A shop-scoped role naming another shop; an organization role with no granted shops; a list request without a shop.
- `aal1` with and without MFA-protected capabilities; `aal1` together with a missing step-up (MFA wins).

## Verification evidence

### Verification — 2.4 (part 1) — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-2.4.1 | 177 tests in `tests/application/test_access_policy.py` | Red first: 113 failing against a deny-everything stub. Drift checks: granting the Viewer buyer contact in code failed 2 tests; granting the Viewer the audit log in the doc failed 2 tests; both reverted. `uv run just check`: domain and application at 100% branch coverage. | Pass |

**Open issues:** none for 2.4 (part 2 merged with the endpoints).
