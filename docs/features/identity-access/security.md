# Identity and access — Security

## Threats (STRIDE), slice 2.4 part 1

| Element | Threat | Control | Test |
|---|---|---|---|
| Authorization | Elevation of privilege: a role reaching a capability it was never granted | Deny by default; every role × capability tested against the documented matrix | `test_decide__fully_assured_principal__…` |
| Authorization | Elevation of privilege: a shop-scoped user reaching another shop | Shop-scoped grants require the shop to be granted | `test_decide__shop_scoped_role__is_denied_a_shop_it_was_not_granted` |
| Authorization | Elevation of privilege: an Owner or Admin of one organization reaching a shop of another | Every targeted shop carries its organization; a mismatch is denied for every role and capability | `test_decide__shop_of_another_organization__is_denied_to_every_role` |
| Authorization | Elevation of privilege: acting on a weak session | MFA and step-up checks; step-up expires after 10 minutes; future timestamps refused | assurance and step-up tests |
| Authorization rules | Tampering: the code or the documentation changed alone | The test parses the documented table; drift in either direction fails | `test_permissions__match_the_documented_matrix` |

## Threats, slice 2.1 (database workflow)

| Element | Threat | Control | Test |
|---|---|---|---|
| Data API | Information disclosure: tables reachable through PostgREST | Only `public` exposed; business schemas closed to `anon`, `authenticated`, `service_role` | `00_schemas.test.sql` |
| Readiness probe | Information disclosure: the connection string in logs or errors | `SecretStr` setting; kept out of `repr`; readiness logs only the error type | `test_database_check__repr__hides_the_connection_string`, `test_main__database_configured__readyz_checks_it` |
| Migrations | Tampering: an agent or a pull request changes the deployed schema | Only `workflow_dispatch` on `main` by the owner; dry run first; secrets only in the `development` environment | workflow review |
| Local stack | Spoofing: local default credentials reused elsewhere | They are the CLI's fixed local values, used only for `127.0.0.1` and CI | review |

## Threats, slice 2.2 (identity schema)

| Element | Threat | Control | Test |
|---|---|---|---|
| Every table | Information disclosure across tenants | RLS on every table, tenant columns in every policy, helper functions keyed on `auth.uid()` | AC-2.2.1, AC-2.2.2 |
| Shops | Elevation of privilege: a shop-scoped role reading another shop | `private.user_shop_ids()`: Owners and Admins see all shops, other roles only granted ones | AC-2.2.3 |
| Shop grants | Tampering: a grant that joins two organizations | Composite foreign keys on `(organization_id, id)` | FK assertion in `01_identity_access.test.sql` |
| Audit log | Repudiation and tampering: editing or deleting records | No update or delete grants; append-only triggers for every role | AC-2.2.4 |
| Owner-administered tables | Elevation of privilege with a stolen password (no second factor) | Restrictive `aal2` policies on every write | AC-2.2.5 |
| Policy helpers | Elevation of privilege through security-definer functions | `search_path = ''`, read-only, keyed on the caller's `auth.uid()`, executable only by `authenticated`, in a schema the Data API does not expose | review, `00_schemas.test.sql` |

## ASVS 5.0 requirements covered

| Requirement | How it is met | Evidence |
|---|---|---|
| v5.0.0-8.1.1 | The permission matrix documents function-level and data-specific (shop) rules, and the code is tested against it | `PERMISSION_MATRIX.md`, matrix test |
| v5.0.0-8.2.1 | Every capability needs an explicit grant | role × capability tests |
| v5.0.0-8.2.2 (decision part) | Shop-scoped access requires a grant for that shop; the endpoint and RLS parts arrive with slices 2.2 to 2.4 | shop tests |
| v5.0.0-8.3.1 (partial) | The decision runs on the server, from server-side facts; the endpoints that call it arrive in part 2 | design |
