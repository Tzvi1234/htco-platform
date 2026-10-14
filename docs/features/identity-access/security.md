# Identity and access — Security

## Threats (STRIDE), slice 2.4 part 1

| Element | Threat | Control | Test |
|---|---|---|---|
| Authorization | Elevation of privilege: a role reaching a capability it was never granted | Deny by default; every role × capability tested against the documented matrix | `test_decide__fully_assured_principal__…` |
| Authorization | Elevation of privilege: a shop-scoped user reaching another shop | Shop-scoped grants require the shop to be granted | `test_decide__shop_scoped_role__is_denied_a_shop_it_was_not_granted` |
| Authorization | Elevation of privilege: an Owner or Admin of one organization reaching a shop of another | Every targeted shop carries its organization; a mismatch is denied for every role and capability | `test_decide__shop_of_another_organization__is_denied_to_every_role` |
| Authorization | Elevation of privilege: acting on a weak session | MFA and step-up checks; step-up expires after 10 minutes; future timestamps refused | assurance and step-up tests |
| Authorization rules | Tampering: the code or the documentation changed alone | The test parses the documented table; drift in either direction fails | `test_permissions__match_the_documented_matrix` |

## Threats (STRIDE), slice 2.3

| Element | Threat | Control | Test |
|---|---|---|---|
| Token verification | Spoofing: an unsigned token (`alg: none`) or one signed with HS256 | Only ES256 is accepted, checked in the header and by the decoder | `test_verify__alg_none__is_rejected`, `test_verify__hs256_signed_with_the_public_key__is_rejected` |
| Token verification | Spoofing: a token for another project or audience | Issuer and audience checked | wrong-issuer and wrong-audience cases |
| Token verification | Elevation of privilege: the anon or service key used as a user token | `role` must be `authenticated` | `anon-role` and `service-role` cases |
| Key set | Denial of service: forged key IDs forcing JWKS fetches | Refresh on an unknown key ID at most every 30 seconds | `test_jwks__unknown_kids_within_the_cooldown__do_not_refetch` |
| Key set | Spoofing: keys trusted after the provider revoked them | Keys expire after 10 minutes and are never used stale | `test_jwks__unreachable_after_expiry__does_not_use_stale_keys` |
| Authorization | Elevation of privilege: roles taken from token claims or user metadata | Memberships read from the database on every request, inside the user's RLS context | application and integration tests |
| Database | Information disclosure: a query without a tenant filter | User-scoped transactions as `authenticated` with the verified claims; RLS returns only the tenant's rows | `test_unfiltered_query__returns_only_the_users_tenant_rows` |
| Logs | Information disclosure: tokens in logs | Rejections log only the problem type | `test_rejections__are_logged_with_the_reason_and_never_the_token` |

## ASVS 5.0 requirements covered

| Requirement | How it is met | Evidence |
|---|---|---|
| v5.0.0-8.1.1 | The permission matrix documents function-level and data-specific (shop) rules, and the code is tested against it | `PERMISSION_MATRIX.md`, matrix test |
| v5.0.0-8.2.1 | Every capability needs an explicit grant | role × capability tests |
| v5.0.0-8.2.2 (decision part) | Shop-scoped access requires a grant for that shop; the endpoint and RLS parts arrive with slices 2.2 to 2.4 | shop tests |
| v5.0.0-8.3.1 (partial) | The decision runs on the server, from server-side facts; the endpoints that call it arrive in part 2 | design |
