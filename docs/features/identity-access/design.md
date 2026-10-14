# Identity and access — Design

## Policy engine v0 (slice 2.4, part 1)

```mermaid
flowchart TD
  start([decide principal, capability, shop, now]) --> grant{Grant for this role?}
  grant -->|no| deny[DENY]
  grant -->|yes| tenant{Shop of another organization?}
  tenant -->|yes| deny
  tenant -->|no| shop{Shop-scoped grant and a shop not granted?}
  shop -->|yes| deny
  shop -->|no| mfa{Needs MFA and session is aal1?}
  mfa -->|yes| mfareq[MFA_REQUIRED]
  mfa -->|no| step{Needs step-up and no re-authentication in the last 10 minutes?}
  step -->|yes| stepreq[STEP_UP_REQUIRED]
  step -->|no| allow[ALLOW with scope, via_approval, masked]
```

| Type | Layer | Meaning |
|---|---|---|
| `Role`, `AssuranceLevel` | `htco_domain.identity` | The seven roles; `aal1` or `aal2` |
| `Capability` | `htco_application.access.policy` | The 20 human capabilities of the matrix |
| `Grant` | same | One matrix cell: organization-wide or granted shops; MFA; step-up; approval instead of acting; masked |
| `Principal` | same | The acting user as the server knows them: organization, role, assurance, granted shops (from the database, never from token claims alone) |
| `ShopRef` | same | The targeted shop and its owning organization, read from the database |
| `Decision` | same | `ALLOW` (with scope and flags), `DENY`, `MFA_REQUIRED` or `STEP_UP_REQUIRED` |
| `PERMISSIONS` | same | The matrix, read-only (`MappingProxyType`) |

How the API will map decisions (part 2): `DENY` on a specific resource is 404 (no hint it exists), on a function 403; `MFA_REQUIRED` and `STEP_UP_REQUIRED` are 403 problems with their own types (`mfa-required`, `step-up-required`); `via_approval` creates an approval instead of acting; `masked` hides buyer data.

## Decisions and alternatives

| Decision | Alternatives considered | Why |
|---|---|---|
| The matrix as a literal table in code, checked against the Markdown table | Generate code from the doc; read the doc at runtime | A reviewed table in code is simple and fast; the parsing test makes any drift between doc and code fail CI. |
| The target shop carries its organization, and a mismatch is denied for every role | Trust the caller to pre-filter by tenant | An organization-wide grant must never cross tenants; the engine checks it itself instead of relying on every caller (and RLS adds a second layer). |
| Assurance checked before step-up | Either order | A step-up re-enters the second factor, so it presupposes an `aal2` session. |
| A re-authentication time in the future counts as none | Trust it | Clock skew or tampering must never extend an elevation. |
| A naive clock is refused | Assume UTC | Time is UTC and injected (golden rule 3); a naive time is a bug to surface. |
