# Policy engine — Design

## Modules

| Layer | Module | Role |
|---|---|---|
| Domain | `htco_domain.policy` | `PolicyDocument` (the capability catalogue and the grants), `ActorGrants`, `CapabilitySpec`, `PolicyVersion`, `PolicyDecision`, `Reason` and `Verdict` |
| Application | `htco_application.access.engine` | `evaluate` (pure), `PolicyRequest`, `HUMAN_CAPABILITIES`, `baseline_policy(registry)` |
| Application | `htco_application.policies` | `PolicyService`: `active`, `ensure_baseline`, `publish` (Owner only) and `decide`, which records each decision |
| Persistence | `htco_persistence.policies` | `PostgresPolicyStore` on `app.policies` and `app.policy_decisions` |

## The document

```json
{
  "capabilities": {"etsy.receipts.read": {"side_effect": "none", "risk": "low"}},
  "employees":   {"maya": {"capabilities": ["tasks.update"], "pre_approved": [], "autonomy": "L1"}},
  "automations": {"etsy-order-intake": {"capabilities": ["etsy.receipts.read"], "pre_approved": [], "autonomy": null}},
  "systems":     {}
}
```

Parsing is strict: a missing or unknown key is rejected. The document is also checked as a whole:

- every grant names a capability in the catalogue;
- only employees carry an autonomy level;
- every pre-approved capability is also granted;
- only an L3 employee, an automation or a system component can hold a pre-approval;
- an external or financial capability is at least medium risk.

**Versions.** A published version is immutable. `publish` inserts the next version under a per-organization advisory lock and writes the `policies.published` audit record in the same transaction. To revert, publish an older document again; it becomes a new version. Version 1 is the baseline, built from the registry. It contains:

- the human capabilities and every tool in the catalogue;
- each production-ready automation, with its `allowed_actions`;
- each production-ready employee, with its `allowed_tools` and autonomy level;
- no pre-approvals and no system grants.

## Evaluation order

1. No policy → `no_policy`. An advisor → `advisor_has_no_capabilities`.
2. A policy or a shop of another organization → `other_organization`.
3. A capability that is not in the catalogue → `unknown_capability`.
4. **A user** is decided by the v0 matrix:
   - not a member → `unknown_actor`;
   - no cell, or a non-human capability → `not_granted`;
   - a cell, but the shop is outside the user's granted shops → `shop_not_granted`;
   - MFA or step-up missing → `mfa_required` or `step_up_required`;
   - an `A` cell → `role_requires_approval`;
   - otherwise → `granted`.
5. **An employee, automation or system component** is decided by its own grants:
   - not in its section → `unknown_actor`;
   - the capability is not granted → `not_granted`;
   - for an employee acting on a shop: without an originating user → `no_originating_user`; the user is of another organization → `other_organization`; the user cannot reach the shop → `shop_not_granted`. Owner and Admin reach every shop of the organization.
   - for an employee without a shop: an originating user of another organization → `other_organization`.
6. Side effect and risk, which apply only to the identities in step 5:
   - an L0 employee with any side effect → `autonomy_too_low`;
   - critical risk → `critical_risk`;
   - external or financial → `pre_approved` if the identity holds a pre-approval, else `acts_outside_htco`;
   - otherwise → `granted`.

These rules match `decide_tool_access` (ADR-0014), which already requires an approval for external tools below L3. The one stricter rule is the matrix legend's "L0 suggests only".

The reason fixes the verdict, in the domain and in a database check. A decision without a version exists only for `no_policy`.

## What a decision reaches

Every decision also says what it reaches (`PolicyDecision.shops`, `masked`; quill2 on #189), so a list or an action without a shop is narrowed as the matrix says:

- a request on a shop reaches that shop;
- without a shop, **a user** reaches v0's scope: every shop for an organization-wide cell (`✓`), the granted shops for an `S` cell, with `(masked)` as `masked`;
- without a shop, **an employee** reaches what the originating user reaches: every shop for an Owner or Admin, the granted shops for any other role, none without an originating user;
- without a shop, **an automation or a system component** reaches every shop of its organization;
- `shops` is `None` for every shop of the organization and empty for none; a denial reaches nothing, and the domain refuses one that claims more.

The recorded decision keeps the verdict, the reason and the version; what it reached follows from them and the policy version. Callers build `PolicyRequest` through `htco_application.policies`, the engine's only entry point.

## Why `policy_decisions` and not `audit.audit_log`

There is one decision per evaluated request, so the table has high volume and a fixed shape. A dedicated table can be indexed and has a reason/verdict check. The audit log keeps the sensitive changes, such as publishing a policy.
