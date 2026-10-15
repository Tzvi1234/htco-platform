# Identity and access — Security

## Threats (STRIDE), slice 2.4 part 1

| Element | Threat | Control | Test |
|---|---|---|---|
| Authorization | Elevation of privilege: a role reaching a capability it was never granted | Deny by default; every role × capability tested against the documented matrix | `test_decide__fully_assured_principal__…` |
| Authorization | Elevation of privilege: a shop-scoped user reaching another shop | Shop-scoped grants require the shop to be granted | `test_decide__shop_scoped_role__is_denied_a_shop_it_was_not_granted` |
| Authorization | Elevation of privilege: an Owner or Admin of one organization reaching a shop of another | Every targeted shop carries its organization; a mismatch is denied for every role and capability | `test_decide__shop_of_another_organization__is_denied_to_every_role` |
| Authorization | Elevation of privilege: acting on a weak session | MFA and step-up checks; step-up expires after 10 minutes; future timestamps refused | assurance and step-up tests |
| Authorization rules | Tampering: the code or the documentation changed alone | The test parses the documented table; drift in either direction fails | `test_permissions__match_the_documented_matrix` |

## Threats (STRIDE), slice 2.6

| Element | Threat | Control | Test |
|---|---|---|---|
| Management endpoints | Elevation of privilege: a non-admin role changing access | `manage_access` only for Owner and Admin, with `aal2`; RLS restrictive `aal2` policies refuse the write even if the application were bypassed | AC-2.6.1 tests, integration RLS test |
| Management endpoints | Elevation of privilege: an Admin making themselves or someone Owner, or removing the Owner | Only the Owner grants the Owner role; the Owner's membership is not changeable through the API | AC-2.6.2 tests |
| Sessions | Elevation of privilege: a removed member keeps working until the token expires | Memberships are read from the database on every request | AC-2.6.3 tests |
| Management endpoints | Repudiation: a change without a trace | One audit record per change, by the acting user, in the same transaction, with the request ID | AC-2.6.4 tests |
| Secret key | Information disclosure or misuse: the Supabase secret key | Used only for the Auth admin `generate_link` invitation call, never for database queries; hidden in `repr`; optional (invitations off without it) | `test_inviter__repr__hides_the_secret_key`, composition tests |
| Invitation link | Information disclosure or spoofing: the one-time link lets whoever holds it set the invitee's password | Returned only to Owner or Admin with `aal2`; `Cache-Control: no-store`; never logged, audited or stored; hidden from `repr`; one use, expires with Supabase's email OTP expiry; lands only on the allow-listed redirect | `test_invite__returns_the_one_time_link_uncached_and_unlogged`, `test_invited__repr__hides_the_link`, `test_invite__returns_the_one_time_link_for_the_inviter_to_pass_on` |
| Tenancy | Information disclosure: probing other organizations' members and shops | Members, shops and grants outside the organization are 404 | not-found tests |

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
