# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Email **security@office.ht-co.app** with a description, steps to reproduce and the impact you expect. We aim to acknowledge reports within 3 business days and to share a remediation plan within 10 business days.

Until that mailbox is live (inbound mail arrives in Phase 12), contact the repository owner directly through GitHub.

## Scope

This repository contains the HTCO platform: API, workers, agent runtime, database migrations and infrastructure configuration. Client applications are covered by the `htco-clients` repository's policy.

## Our security baseline

- OWASP ASVS 5.0 Level 2, plus selected Level 3 requirements for authorization, OAuth scopes, high-value approvals and concurrency.
- OWASP Top 10 for Agentic Applications (2026) controls for every agent workflow.
- Tenant isolation enforced in the API and by row-level security, verified by automated cross-tenant tests.
- Secrets are never stored in the repository; gitleaks runs in a pre-commit hook and in CI on every push.
- Dependencies and GitHub Actions are pinned and scanned.

Details: [threat model](docs/architecture/THREAT_MODEL.md) and [engineering rules](docs/engineering/ENGINEERING_RULES.md#7-security-er-sec).
