# Policy engine — Operations

There are no settings. The migration `20261027110000_policies.sql` creates `app.policies` and `app.policy_decisions`. It is additive and safe on the live database.

- **The first policy.** `PolicyService.ensure_baseline(organization, baseline_policy(registry))` publishes version 1 once, as `system:policy-baseline`. Until an organization has a policy, every decision is `DENY` with reason `no_policy`.
- **Reverting.** Publish the previous document again. It becomes the next version, and the decisions made under the version being reverted keep their version.
- **Reading the trail.** `app.policy_decisions` by `(organization_id, decided_at desc)`, or by `(organization_id, policy_version)`. Publishing appears in `audit.audit_log` as `policies.published`.
- **Rollback.** The tables are additive. Nothing reads them yet except the service, so a code rollback leaves them unused.
