## What and why

<!-- One paragraph in plain language. Link the slice (docs/plan/phases/…) or issue. -->

Slice / issue:

## Acceptance criteria

<!-- List the AC IDs this PR covers and where each is tested. -->

| AC | Tests |
|---|---|
| | |

## How it was verified

<!-- Summary of the feature-verification evidence (also recorded in docs/features/<feature>/tests.md). -->

## Test changes

<!-- Required if any existing test, assertion, threshold or skip marker changed. Explain why the old expectation was wrong. Otherwise write "None". -->

## Checklist

- [ ] Tests were written first and cover every acceptance criterion
- [ ] `just check` passes locally; CI is green
- [ ] Feature docs updated (`overview`, `design`, `tests`, `security`, `operations` as relevant)
- [ ] Contracts updated (OpenAPI, events) and generated artifacts committed
- [ ] DATA_MODEL / EVENT_CATALOG / PERMISSION_MATRIX / ADR updated if affected
- [ ] Slice ticked in its phase file

## Security

- [ ] No secrets, tokens or personal data in code, fixtures, logs or this PR
- [ ] Tenant isolation: every new query or endpoint is tenant-scoped and covered by a cross-tenant test
- [ ] Inputs validated at the boundary; errors return problem+json without internals
- [ ] External side effects go through the outbox with an idempotency key
- [ ] Agent changes: tools allowlisted, policy-checked, Rule of Two respected
- [ ] Migrations are forward-compatible and have pgTAP tests
- [ ] New dependencies justified (license, maintenance, pinned)

ASVS 5.0 requirements touched:

## Risk and rollback

<!-- What could go wrong in production, and how to turn it off or roll it back. -->

## Cost impact

<!-- New infrastructure, model calls or polling? Expected monthly cost. Otherwise "None". -->
