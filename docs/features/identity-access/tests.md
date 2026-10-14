# Identity and access — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Database (pgTAP) | Base schemas exist; `anon` and `authenticated` cannot use `private` or create in `app` and `audit` | `supabase/tests/database/00_schemas.test.sql` |
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
| AC-2.4.1 | `test_permissions__match_the_documented_matrix` (20 cases), `test_decide__fully_assured_principal__gets_exactly_the_documented_answer` (140 cases) |
| AC-2.4.2, AC-2.4.3 | Part 2 |

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

**Open issues:** AC-2.4.2 and AC-2.4.3 (endpoints, part 2).
