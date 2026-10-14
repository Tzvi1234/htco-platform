# Identity and access — Security

## Threats (STRIDE), slice 2.4 part 1

| Element | Threat | Control | Test |
|---|---|---|---|
| Authorization | Elevation of privilege: a role reaching a capability it was never granted | Deny by default; every role × capability tested against the documented matrix | `test_decide__fully_assured_principal__…` |
| Authorization | Elevation of privilege: a shop-scoped user reaching another shop | Shop-scoped grants require the shop to be granted | `test_decide__shop_scoped_role__is_denied_a_shop_it_was_not_granted` |
| Authorization | Elevation of privilege: an Owner or Admin of one organization reaching a shop of another | Every targeted shop carries its organization; a mismatch is denied for every role and capability | `test_decide__shop_of_another_organization__is_denied_to_every_role` |
| Authorization | Elevation of privilege: acting on a weak session | MFA and step-up checks; step-up expires after 10 minutes; future timestamps refused | assurance and step-up tests |
| Authorization rules | Tampering: the code or the documentation changed alone | The test parses the documented table; drift in either direction fails | `test_permissions__match_the_documented_matrix` |

## ASVS 5.0 requirements covered

| Requirement | How it is met | Evidence |
|---|---|---|
| v5.0.0-8.1.1 | The permission matrix documents function-level and data-specific (shop) rules, and the code is tested against it | `PERMISSION_MATRIX.md`, matrix test |
| v5.0.0-8.2.1 | Every capability needs an explicit grant | role × capability tests |
| v5.0.0-8.2.2 (decision part) | Shop-scoped access requires a grant for that shop; the endpoint and RLS parts arrive with slices 2.2 to 2.4 | shop tests |
| v5.0.0-8.3.1 (partial) | The decision runs on the server, from server-side facts; the endpoints that call it arrive in part 2 | design |
