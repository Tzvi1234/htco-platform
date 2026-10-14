# Phase 19 — Engineering department and self-improvement

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | [Phase 15](phase-15-office-executive.md) and [Phase 18](phase-18-optimization.md) |
| **Requirements** | FR-ADM-04, FR-AGT-09, FR-ENG-01, FR-ENG-02, FR-ENG-03, FR-INC-04 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 19.1 Engineering requests and their links
- [ ] 19.2 GitHub integration with least privilege
- [ ] 19.3 Ownership and deduplication
- [ ] 19.4 Engineering safety controls
- [ ] 19.5 Amir activated: reliability and remediation
- [ ] 19.6 Alex activated: software and architecture
- [ ] 19.7 Iris activated: agent systems, tools and office experience
- [ ] 19.8 Engineering Console actions
- [ ] 19.9 Scenario I

## Goal

The office can maintain and improve itself without ever bypassing review, CI, approvals or deployment gates. Every change starts as an **engineering request** linked to its GitHub issue, branch, pull request, tests and deployment records. Requests come from the owner, Maya, Noa's findings, incidents, Omer's proposals, agent improvement proposals, monitoring and UX opportunities. Three engineering employees work them with clear ownership: **Alex** (Lead Software & Platform Engineer: software and architecture), **Amir** (Debugger & Reliability Engineer: incidents, allowlisted reversible remediations, regression tests) and **Iris** (Agent Systems & Experience Engineer: employee definitions, skills, tools, evaluations, office UX).

## Why now

Self-improvement is the riskiest capability of the office. It comes after supervision (Phase 10), the office and its Engineering Console (Phase 15) and measured optimization (Phase 18), so that every change it makes is visible, measured and gated.

## Rules (from [ADR-0012](../../adr/0012-living-office-organization.md) rule 6 and FR-ENG-03)

- No engineering employee may weaken tests, remove logging, widen its own permissions or bypass branch protection.
- No production deployment without the owner's release gate.
- Changes to code, databases, permissions, billing, integrations or production follow review, CI, staged release and feature flags.
- Safe internal proposals and dry-runs may be automated (opening an issue, drafting a pull request, running tests on a branch, a migration dry-run).
- One request has one owner; no duplicate implementations.

## Scope

**In:** `engineering_requests` and `engineering_changes` (links to issues, branches, pull requests, CI runs, deployments; source; owner; state); a GitHub App with the minimum permissions for issues, branches and pull requests, using a machine account (D-08); webhook ingestion of pull request, CI and deployment events through the inbox; request routing to Alex, Amir or Iris with deduplication; safety controls in CI and in the tool bridge; Amir's incident workflow with `remediation.run_allowlisted` and `debug_reports`; activation of Alex, Amir and Iris; safe actions in the Engineering Console; Scenario I.

**Out:** autonomous merges or deployments (never); changes to the owner's release gate.

## Owner prerequisites

- D-08 decided (machine account for agent pull requests); GitHub App installed with the agreed permissions.
- The remediation allowlist reviewed (reversible, idempotent actions only).
- Approval of Alex's, Amir's and Iris's autonomy levels (proposed L1 each).

## Employees activated

| Employee | Registry | Owns |
|---|---|---|
| Alex, Lead Software & Platform Engineer | [`alex.toml`](../../../registry/employees/alex.toml) | Features, automation workflows, APIs, infrastructure improvements, approved Omer proposals, migrations, release plans |
| Amir, Debugger & Reliability Engineer | [`amir.toml`](../../../registry/employees/amir.toml) | Technical incidents, root causes, allowlisted reversible repairs, fixes with regression tests, recovery verification |
| Iris, Agent Systems & Experience Engineer | [`iris.toml`](../../../registry/employees/iris.toml) | Employee definitions, skills, tool interfaces, prompts and evaluations, test environments for agent behaviour, office UX |

Each follows the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7. Amir also becomes the business owner of Incident Detection & Escalation (active since [Phase 6](phase-06-fulfillment-incidents.md)) and takes over remediation from the allowlisted code and people of Phases 6–18.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 19.1 | `engineering_requests` and `engineering_changes` with sources and links | Every merged pull request of the platform links to a request; every request shows its issue, branch, PR, CI and deployment state |
| 19.2 | GitHub App with least privilege; pull request, CI and deployment events through the inbox | The App cannot change branch protection, rulesets, secrets or workflow permissions (verified by its permission manifest) |
| 19.3 | Ownership and deduplication: rules route requests to Alex, Amir or Iris; similar open requests are linked, not duplicated | One open request per problem; a second detection adds evidence to the first |
| 19.4 | Safety controls: test-integrity job (Phase 1) extended to logging removal; registry changes that touch an employee's own grants, autonomy or routing need the owner's review (CODEOWNERS); tool bridge denies merge and deploy tools to every employee | An employee's pull request that weakens a test, removes a log statement or widens its own permissions fails CI or review |
| 19.5 | Amir: incident investigation, allowlisted remediations, debug reports, regression tests | Only allowlisted, reversible, idempotent remediations run; anything else becomes an approval or a request; incidents resolve only on evidence |
| 19.6 | Alex: requests to pull requests with tests, migrations and docs | Every PR has failing-tests-first evidence and passes CI; no merge without review |
| 19.7 | Iris: changes to `registry/`, skills, tool definitions and evaluations through pull requests; office UX improvements | A profile change creates a new version only through review and re-evaluation; permission, budget or autonomy changes need the owner's approval |
| 19.8 | Engineering Console actions (web, and Windows in [Phase 17](phase-17-windows.md)): approve a release gate, retry a dead letter, run a dry-run | Every action goes through the API's checks, needs step-up where high-risk, and is audited |
| 19.9 | Scenario I | See below |

| ID | Acceptance criterion |
|---|---|
| AC-19.9.1 | **Scenario I** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)): an engineering employee's fix for a production problem cannot reach production without CI, permissions, pull request review and the owner's deployment gate. Each bypass attempt (pushing to `main`, merging its own PR, skipping a failing test, widening its own grants, deploying directly, running a non-allowlisted remediation) is refused and recorded, and the legitimate path (request, branch, PR, CI, review, staged release, owner gate) succeeds. |

## Test plan

Policy and tool-bridge tests for every engineering tool; CI tests for the extended integrity checks; GitHub App permission checks; evaluation sets for Alex (small, well-specified changes), Amir (incident diagnosis from recorded traces) and Iris (profile and tool changes with evaluation effects); Scenario I on the real repository with a sandbox branch.

## Security checklist

- [ ] GitHub App and machine account with least privilege; no admin rights; tokens short-lived.
- [ ] Branch protection and the release gate cannot be changed by any employee or by the App.
- [ ] Remediation allowlist reviewed by the owner; every remediation reversible and idempotent.
- [ ] Repository content, issues and logs are untrusted input for the engineering employees.

## Documentation

`docs/features/engineering-requests/`, `docs/features/employees/alex.md`, `docs/features/employees/amir.md`, `docs/features/employees/iris.md`; ENGINEERING_RULES updated for employee-authored pull requests; THREAT_MODEL updated for self-modification risks.

## Exit gate

- [ ] Scenario I passes; at least one real request from each source kind handled end to end.
- [ ] Alex, Amir and Iris are `ready`, their autonomy approved, their routing measured.

## Rollback

Kill switches per engineering employee; the GitHub App can be suspended by the owner; every change already follows the normal revert and rollback path.

## Risks

| Risk | Mitigation |
|---|---|
| An employee finds a path around a gate | Gates enforced by GitHub and CI, not by prompts; Scenario I re-run at every release |
| Review load on the owner | Small pull requests; Noa samples engineering changes; low-risk changes batched |

## Cost impact

Model usage of the three engineering employees, measured per request; GitHub machine account if needed (D-08).
