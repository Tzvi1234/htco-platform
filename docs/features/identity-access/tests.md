# Identity and access — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Application | The documented matrix parsed from `PERMISSION_MATRIX.md` equals `PERMISSIONS`; every role × capability (140 cases) for a fully assured user; shop scope (granted, not granted, organization-wide, another organization's shop for every role × capability, lists); masked and approval grants; MFA; step-up window boundaries; check order; naive clocks | `tests/application/test_access_policy.py` |

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
| AC-2.1.1 | CI `database` job: `supabase db start` applies every migration on a fresh runner |
| AC-2.1.2 | CI `database` job: `supabase test db` (`supabase/tests/database/00_schemas.test.sql`); a deliberately failing assertion was pushed and failed the job (see the pull request) |
| AC-2.1.3 | `tests/persistence/test_readiness.py::test_readyz__database_goes_away_and_comes_back__503_then_200` (database), `test_database_check__nothing_listening__raises`; `tests/api/test_main.py::test_main__database_configured__readyz_checks_it`, `test_main__deployed_environment_without_database__exits_with_configuration_error` |
| AC-2.1.4 | `.github/workflows/migrate.yml` (actionlint and zizmor clean); first run when the development project exists |
| AC-2.2.1 to AC-2.2.5 | `supabase/tests/database/01_identity_access.test.sql` (27 assertions: inventory; A sees nothing of B in six tables, with positive controls; Support shop scope; audit update and delete refused for the API role and for the table owner; unknown actor kind refused; aal1 writes refused and aal2 writes allowed; cross-organization grant refused by the composite key; `anon` refused); `tests/persistence/test_schema_matches_domain.py` (domains equal `Role` and `ActorKind`) |
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

### Verification — 2.1 — 2026-10-14

| AC | Evidence | Result |
|---|---|---|
| AC-2.1.1 | CI run 38573775759, job `database`: `supabase db start` on a fresh runner applied every migration | Pass |
| AC-2.1.2 | CI run 38574471491: a deliberately failing assertion (`99_red_demo.test.sql`) failed the `database` job (`Failed test 1 … Looks like you failed 1 test of 1`) and therefore `ci-gate`; reverted in the next commit | Pass |
| AC-2.1.3 | CI run 38573775759, job `database`: `test_readyz__database_goes_away_and_comes_back__503_then_200` against real Postgres; locally the unit and composition-root tests (714 passed) | Pass |
| AC-2.1.4 | `migrate.yml` reviewed and linted (actionlint, zizmor); not run, because no deployed project exists yet | Open until the development project exists |

### Verification — 2.2 — 2026-10-14

| AC | Evidence | Result |
|---|---|---|
| AC-2.2.1 to AC-2.2.5 | CI run 38575026282, job `database`: both migrations and the seed applied on a fresh stack; `Files=3, Tests=39 … Result: PASS` (27 assertions in `01_identity_access.test.sql`) | Pass |
| Enum sync | `tests/persistence/test_schema_matches_domain.py` locally and in CI | Pass |

Local note: Docker Desktop's engine did not start on the development machine during this slice, so the database tests ran only in CI.
