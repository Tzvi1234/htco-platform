# Identity and access — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Application | The documented matrix parsed from `PERMISSION_MATRIX.md` equals `PERMISSIONS`; every role × capability (140 cases) for a fully assured user; shop scope (granted, not granted, organization-wide, another organization's shop for every role × capability, lists); masked and approval grants; MFA; step-up window boundaries; check order; naive clocks | `tests/application/test_access_policy.py` |

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
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
