# Policy engine — Overview

| | |
|---|---|
| **Slug** | `policy-engine` |
| **Phase / slices** | Phase 7: 7.2 (policy engine v1) is built. Step-up rules per capability are 7.4. |
| **Requirements** | [FR-IAM-02, FR-IAM-03, FR-IAM-07](../../product/PRD.md), ER-AI-04, ER-AI-05, ER-AI-17 |
| **Status** | Built as a library with its store. The Action Gateway (`execution/`) and the approvals of 7.3 call it in follow-ups. |
| **Last updated** | 2026-10-27 |

## What and why

One engine decides what any actor may do: a person, an AI employee, an automation, a Decision Advisor or a platform component. It answers `ALLOW`, `REQUIRE_APPROVAL` or `DENY` with a reason, under one version of the organization's policy. Every decision is recorded with that version, so a later change never rewrites what was decided earlier.

- **People** keep the role matrix of policy engine v0 ([PERMISSION_MATRIX](../../architecture/PERMISSION_MATRIX.md) section 1, `htco_application.access.policy`). That matrix changes only through a reviewed pull request.
- **Employees, automations and system components** each have their own grants in the versioned policy document, never a user's (FR-IAM-07). When an employee acts on a shop, it is narrowed further by the originating user's shop access.
- **Advisors** have no section in the document, so nothing can grant them a capability (ADR-0015).

## Acceptance criteria

| ID | Criterion |
|---|---|
| AC-7.2.1 | Unknown capability or actor → `DENY`. |
| AC-7.2.2 | A table-driven test covers every row of the permission matrix. |
| AC-7.2.3 | Changing a policy creates a new version; old decisions keep their version. |
| AC-7.2.4 | Employees and automations are evaluated with their own permission sets, never a user's. An automation acts only under its service identity. An employee's request runs with the employee's grants and the originating user's shop access, whichever is narrower. |
| AC-7.2.5 | An `advisor` actor has no capabilities, so any action it requests is `DENY`. |

## Follow-ups

- The Action Gateway's authorization in `execution/` should call `PolicyService.decide` instead of its own checks. This waits for orion's `execution/` lock.
- 7.3 uses the engine to create approvals for `REQUIRE_APPROVAL`. 7.4 adds the step-up rule per capability to the document.
- An API to read and publish policies (Owner, MFA, step-up) comes with the office screens.
