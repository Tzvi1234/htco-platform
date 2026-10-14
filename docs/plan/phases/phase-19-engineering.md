# Phase 19 — Engineering department and self-improvement

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both (`htco-platform`: engineering runner, requests, GitHub integration, policies; `htco-clients`: Engineering Console actions on web and Windows) |
| **Size** | L |
| **Depends on** | [Phase 16](phase-16-web-office.md) and [Phase 14](phase-14-governance.md) |
| **Requirements** | FR-ADM-04, FR-AGT-09, FR-ENG-01, FR-ENG-02, FR-ENG-03, FR-INC-04, FR-OPT-02, FR-OPT-03; ER-AI-18 |
| **Mandatory tests owned** | **Test 6**, pull-request part (an engineering employee's pull request with the equivalence test); **Test 9** on the real engineering runner and real GitHub (Phase 6 proved the policy with fakes) ([PLAN §5](../PLAN.md#5-mandatory-tests-of-the-master-plan)) |
| **Status** | PLANNED |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The criteria below are the minimum.

## Progress

- [ ] 19.1 Engineering requests and their links
- [ ] 19.2 GitHub App with least privilege, rulesets and event ingestion
- [ ] 19.3 The engineering runner
- [ ] 19.4 Engineering safety controls
- [ ] 19.5 Ownership and deduplication
- [ ] 19.6 Amir activated: reliability and remediation
- [ ] 19.7 Alex activated: software, architecture and Omer's proposals
- [ ] 19.8 Iris activated: agent systems, tools and office experience
- [ ] 19.9 Engineering Console actions
- [ ] 19.10 Scenario I and mandatory tests 6 and 9 on the real runner

## Goal

The office can maintain and improve itself without ever bypassing review, CI, approvals or deployment gates. Every change starts as an **engineering request** linked to its GitHub issue, branch, pull request, tests and deployment records. Requests come from the owner, Maya, Noa's findings, incidents, Omer's proposals, agent improvement proposals, monitoring and UX opportunities. Three engineering employees work them with clear ownership: **Alex** (Lead Software & Platform Engineer: software and architecture), **Amir** (Debugger & Reliability Engineer: incidents, allowlisted reversible remediations, regression tests) and **Iris** (Agent Systems & Experience Engineer: employee definitions, skills, tools, evaluations, office UX).

Code work runs on the **engineering runner** of [ADR-0016](../../adr/0016-claude-native-agent-runtime.md) ("Engineering employees"): a separate, sandboxed process where Claude's built-in code tools are enabled only inside an isolated Git worktree, with a GitHub credential that can only push branches and open pull requests, no production credentials and no way to merge. This runner is never used for business employees.

## Why now

Self-improvement is the riskiest capability of the office. It comes after full governance ([Phase 14](phase-14-governance.md): Noa's supervision and Omer's measured optimization pipeline) and the complete web office with its Engineering Console ([Phase 16](phase-16-web-office.md)), so that every change it makes is visible, measured and gated. Omer's pilot ([Phase 7](phase-07-pilot-office.md)) already opens improvement issues on GitHub; this phase gives them owners who can do the work.

## Rules

From [ADR-0012](../../adr/0012-living-office-organization.md) rule 6, FR-ENG-03, [ER-AI-18](../../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai) and the master plan §16:

- The path is always: request → issue → isolated branch → pull request → CI → staging → review → approval by risk → release → post-deploy measurement.
- No engineering employee may weaken tests, remove logging, widen its own permissions, approve its own work or bypass branch protection.
- No merge and no production deployment by any employee. Merging and the release gate stay with the owner.
- Changes to code, databases, permissions, billing, integrations or production follow review, CI, staged release and feature flags.
- Safe internal proposals and dry-runs may be automated (opening an issue, drafting a pull request, running tests on a branch, a migration dry-run).
- One request has one owner; no duplicate implementations.
- Repository content, issues, pull request comments, CI logs and traces are untrusted input.

## Scope

**In:**

- `engineering_requests` (extended from Phase 7) and `engineering_changes` (source, owner, state; links to issues, branches, pull requests, CI runs, deployments and the optimization proposal or incident that caused them); events `engineering.request_created.v1` and `engineering.change_deployed.v1`.
- A GitHub App with minimum permissions on `htco-platform` and `htco-clients`, acting as the machine identity of D-08 and D-15; repository rulesets that make `main`, release branches and tags unreachable for it; webhook ingestion of issue, pull request, check and deployment events through the inbox.
- The **engineering runner**: a separate DigitalOcean worker component, with its own secrets, its own kill switch and its own budget; per-step isolated worktree; locked-down Claude Agent SDK options for code work; a controller outside the session that holds the GitHub credential and is the only part that pushes and opens pull requests.
- Two kinds of steps for engineering employees: **office steps** on the normal Claude runtime with HTCO tools only (triage, reading logs and traces through `observability.*`, creating requests, commenting), and **code steps** on the engineering runner.
- Safety controls in CI, CODEOWNERS, rulesets and the tool bridge.
- Request routing to Alex, Amir or Iris with deduplication.
- Amir's incident workflow with `remediation.run_allowlisted` and `debug_reports`; Amir becomes the business owner of Incident Detection & Escalation (active since [Phase 10](phase-10-fulfillment-incidents.md)).
- Activation of Alex, Amir and Iris.
- Safe actions in the Engineering Console on web and Windows ([Phase 18](phase-18-windows.md)).
- Scenario I; mandatory test 6 (pull-request part) and mandatory test 9 on the real runner.

**Out:** autonomous merges or deployments (never); changes to the owner's release gate (never); business employees on the engineering runner (never); Claude Code agent teams or cross-session messaging as a product dependency ([ADR-0016](../../adr/0016-claude-native-agent-runtime.md)); Claude Managed Agents for the runner (possible later, with an ADR).

## Owner prerequisites

- D-08 and D-15 decided (machine identity for agent pull requests; merging and releasing stay with the owner).
- GitHub plan that supports rulesets on private repositories (Phase 1's ruleset is BLOCKED on GitHub Pro); rulesets active on both repositories before any agent pushes.
- GitHub App created and installed with the agreed permissions; the owner keeps the App's private key and is the only one who can change its permissions.
- The remediation allowlist reviewed (reversible, idempotent actions only).
- A budget for engineering model usage (part of D-06).
- Approval of Alex's, Amir's and Iris's autonomy levels (proposed L1 each, as in the registry).
- Decision on network egress for the runner's sandbox (see 19.3) once the options are verified.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`; for 19.9 in the clients repository also `design-system`.
2. Read this file, then [PLAN.md](../PLAN.md) (§5 tests 6 and 9), [ADR-0016](../../adr/0016-claude-native-agent-runtime.md) (all of it, especially "Engineering employees" and the locked-down options), [ADR-0017](../../adr/0017-auth-and-billing-policy.md), [ENGINEERING_RULES](../../engineering/ENGINEERING_RULES.md) (ER-AI, ER-SEC, ER-TEST-02), [THREAT_MODEL](../../architecture/THREAT_MODEL.md), [TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios) (SCN-I), the registry files of Alex, Amir and Iris, and the Phase 6, 7 and 14 feature docs for the runtime, Omer's proposals and Noa's findings.
3. Verify at kickoff, against current official documentation, every external fact this phase relies on: Claude Agent SDK built-in tool names and permission behaviour, GitHub App permission names, ruleset capabilities and bypass rules, installation token lifetime, and DigitalOcean worker isolation and egress options. Record them with dates in the ADR-0016 validation log or a new ADR for the runner.
4. Order: 19.1 → 19.2 → 19.3 → 19.4 (the runner is not used for real work until 19.4 passes) → 19.5 → 19.6 → 19.7 → 19.8 → 19.9 → 19.10. Activate one employee at a time with the activation process of [PLAN §1](../PLAN.md#1-how-a-phase-works).
5. Run the runner first against a sandbox repository with the same rulesets, then against the real repositories.
6. Never give the runner, a session or the App a production credential, a merge capability, an admin permission, the `workflows` permission or a ruleset bypass. Never use `bypassPermissions`. Never let business employees reach the runner.
7. Never weaken a test, CI job, ruleset or CODEOWNERS rule to make a gate pass. If a gate blocks legitimate work, stop and ask.
8. Stop and ask the owner when: a GitHub or DigitalOcean capability needed for isolation does not exist as assumed; a permission broader than this file lists seems necessary; the remediation allowlist needs a new entry; an employee's evaluation misses its threshold; costs exceed the engineering budget.

## Slices

### 19.1 — Engineering requests and their links

[Phase 7](phase-07-pilot-office.md) created `engineering_requests` in minimal form (an approved Omer proposal and its GitHub issue). This slice extends it, without rebuilding it: sources, owner, states, and the new `engineering_changes` table with links to branches, pull requests, CI runs and deployments; use cases and API; activity records; events.

| ID | Acceptance criterion |
|---|---|
| AC-19.1.1 | Every request shows its source (owner, Maya, Noa finding, incident, Omer proposal, agent improvement proposal, monitoring, UX opportunity), owner, issue, branch, pull request, CI and deployment state. |
| AC-19.1.2 | Every merged pull request of both repositories links to a request (CI check on the pull request). |
| AC-19.1.3 | A request created from an Omer proposal moves the proposal to DEVELOPMENT, and the proposal shows the pull request link (FR-OPT-02). |

### 19.2 — GitHub App with least privilege, rulesets and event ingestion

A separate GitHub App for engineering changes; the issue-only App of [Phase 7](phase-07-pilot-office.md) keeps its narrow permissions and is not widened. Engineering App permissions limited to what pushing a branch and opening a pull request need: repository contents (write, for branch pushes), pull requests (write), issues (write), checks and statuses (read), metadata (read). Not granted: administration, workflows, secrets, environments, deployments (write), members, organization permissions. Rulesets on both repositories: `main`, release branches and tags need a pull request with an approving review from a code owner and green required checks; the App is not on any bypass list; agent pushes are allowed only to branches matching `agent/**`. Webhooks for issues, pull requests, checks and deployments through the inbox (signed, deduplicated).

| ID | Acceptance criterion |
|---|---|
| AC-19.2.1 | The App's permission manifest is checked in CI against the list above; any extra permission fails. |
| AC-19.2.2 | With the App's token, a push to `main`, a release branch, a tag or a branch outside `agent/**` is rejected by GitHub (test against the sandbox repository, then the real ones). |
| AC-19.2.3 | The App cannot change branch protection, rulesets, secrets, workflow files or its own permissions (verified by API calls that must fail). |
| AC-19.2.4 | Pull request, check and deployment events update `engineering_changes` exactly once, also when GitHub redelivers them. |

### 19.3 — The engineering runner

A separate DigitalOcean worker component (proposed name `engineering-runner`, code in `workers/engineering/`), never in the same process or component as `agent-worker`. Each code step:

1. The dispatcher of [Phase 5](phase-05-tasks-messages-approvals.md) leases the step. The runner checks that the employee belongs to the engineering department, its profile allows the engineering runner, its kill switch and the runner's switch are on, and the AuthAndBillingPolicy allows `API_PRODUCTION` ([ADR-0017](../../adr/0017-auth-and-billing-policy.md)).
2. The **controller** (runner code outside the session) creates an empty temporary directory, clones the repository at the request's base commit with a read-only token, creates the branch `agent/<employee>/<request-id>`, and installs dependencies from the lockfile. The session gets no network for installs.
3. The **session** starts with: `cwd` = the worktree; built-in code tools enabled (read, search, edit, write, and a shell restricted by the hook); web fetch, web search, subagents and every MCP server other than a small `htco_engineering` server disallowed; `setting_sources=[]`; instructions only from the versioned profile and the request brief; `permission_mode="default"`, never `bypassPermissions`; `max_turns` and `max_budget_usd` from the profile; `env` with only what the binary needs and the Anthropic key. No GitHub token, no Supabase, Etsy, email, DigitalOcean or other production credential is in the environment or the worktree.
4. The **PreToolUse hook** confines every path to the worktree, allows only listed shell commands (test runners, linters, type checkers, `just` recipes, local `git` status, diff, add and commit), denies `git push`, `git remote`, network tools, reading environment variables and anything outside the list, and records every decision. `PostToolUse` writes `tool_invocations` and activity records.
5. The step ends with a terminal tool: `engineering.submit_change` (summary, tests run, evidence) or `engineering.escalate`. The **controller** then checks the diff (branch name, size limit, no binary secrets, protected paths flagged for owner review), pushes the branch and opens or updates the pull request with a short-lived App installation token that it requests for this push only. The session never sees the token.
6. The worktree and the token are deleted. State lives in `engineering_requests`, `task_executions.state_summary`, `agent_runs` and the branch on GitHub; the runner keeps nothing on disk between steps.

Network egress of the sandbox: only the Anthropic API for the session and GitHub for the controller, if DigitalOcean supports it; otherwise shell commands run in a nested sandbox without network (verify the options at kickoff; owner decision).

| ID | Acceptance criterion |
|---|---|
| AC-19.3.1 | A CI test on the runner's session options proves: `setting_sources == []`; no `bypassPermissions`, `acceptEdits` or `auto` mode; web and subagent tools disallowed; `cwd` is a fresh temporary directory; `env` contains no secret other than the Anthropic key. |
| AC-19.3.2 | A business employee profile (any department other than engineering) is refused by the runner, and an engineering code step is refused by `agent-worker`; both refusals are audited. |
| AC-19.3.3 | A session that tries to read or write outside the worktree, run a command outside the list, read environment variables or reach the network is denied by the hook, and the denials are recorded. |
| AC-19.3.4 | Killing the runner during a step leaves no partial push; the lease expires and the step restarts from the request brief and the branch on GitHub, with no duplicate pull request. |
| AC-19.3.5 | Every step writes `agent_runs` with `billing_mode = API_PRODUCTION`, runtime, model, cost from `model_usage`, and the request ID. |

### 19.4 — Engineering safety controls

The Phase 1 test-integrity job extended to detect removed or skipped tests, lowered coverage thresholds and removed log statements; CODEOWNERS makes the owner the required reviewer for `registry/` grants, autonomy and routing, CI workflows, rulesets as code, policies, migrations and the runner itself; the tool bridge denies merge, deploy, ruleset, secret and permission tools to every employee.

| ID | Acceptance criterion |
|---|---|
| AC-19.4.1 | An employee's pull request that weakens a test, removes a log statement or widens its own permissions fails CI or is blocked for owner review. |
| AC-19.4.2 | No employee profile can be granted a merge, deploy, ruleset, secret or permission tool; the registry tests fail if one is added. |
| AC-19.4.3 | The runner's own code, the App manifest and the rulesets cannot be changed by a pull request from the App without the owner's review. |

### 19.5 — Ownership and deduplication

Rules route requests: incidents and regressions to Amir; features, workflows, APIs, migrations, infrastructure and approved Omer proposals to Alex; employee definitions, skills, tools, evaluations, routing tuning and office UX to Iris. Similar open requests are linked, not duplicated. Maya can re-assign with a reason.

| ID | Acceptance criterion |
|---|---|
| AC-19.5.1 | One open request per problem; a second detection adds evidence to the first. |
| AC-19.5.2 | Every request has exactly one owner; a re-assignment is recorded with actor and reason. |

### 19.6 — Amir activated: reliability and remediation

Office steps: incident investigation from logs and traces through HTCO tools, `debug_reports`, allowlisted remediations through `remediation.run_allowlisted`. Code steps: fixes with regression tests on the runner. Amir becomes the business owner of Incident Detection & Escalation.

| ID | Acceptance criterion |
|---|---|
| AC-19.6.1 | Only allowlisted, reversible, idempotent remediations run; anything touching money, permissions, production code or customers becomes an approval or a request (FR-INC-04). |
| AC-19.6.2 | Incidents resolve only on evidence; a fix pull request contains a test that fails before the fix and passes after it. |
| AC-19.6.3 | Amir's evaluation (incident diagnosis from recorded traces) passes its thresholds before activation. |

### 19.7 — Alex activated: software, architecture and Omer's proposals

Requests to pull requests with tests, migrations and docs. Approved Omer proposals to turn a repeated AI action into a deterministic workflow are Alex's.

| ID | Acceptance criterion |
|---|---|
| AC-19.7.1 | Every pull request has failing-tests-first evidence, passes CI and updates its docs; nothing merges without the owner's review. |
| AC-19.7.2 | **Mandatory test 6 (pull-request part).** An approved Omer proposal becomes a request owned by Alex; Alex's pull request implements the workflow and an **equivalence test** that replays recorded inputs of the AI action and shows the workflow's outputs match within the proposal's tolerance; CI is green; after the owner merges and releases, the proposal moves to MEASURING and Phase 14's measurement records the realized savings (VERIFIED or ROLLED_BACK). Omer never implements or approves his own proposal (FR-OPT-03). |
| AC-19.7.3 | Alex's evaluation (small, well-specified changes from a dataset of past requests) passes its thresholds before activation. |

### 19.8 — Iris activated: agent systems, tools and office experience

Changes to `registry/`, skills, tool definitions, evaluations and office UX through pull requests.

| ID | Acceptance criterion |
|---|---|
| AC-19.8.1 | A profile change creates a new profile version only through review and re-evaluation; permission, budget, autonomy, runtime or routing changes need the owner's approval (versioned, as in mandatory test 12). |
| AC-19.8.2 | Iris cannot change her own profile, grants or the runner's configuration without the owner's review (CODEOWNERS plus bridge test). |
| AC-19.8.3 | Iris's evaluation (profile and tool changes with their evaluation effects) passes its thresholds before activation. |

### 19.9 — Engineering Console actions

Web ([Phase 16](phase-16-web-office.md)) and Windows ([Phase 18](phase-18-windows.md)): approve a release gate, retry a dead letter, run a dry-run (tests on a branch, migration dry-run), re-run CI, link or re-assign a request.

| ID | Acceptance criterion |
|---|---|
| AC-19.9.1 | Every action is an API call checked by the policy engine, needs step-up where high-risk, and writes an audit record. |
| AC-19.9.2 | No console action can merge a pull request, deploy to production outside the owner's release gate, or run a free-form command. |

### 19.10 — Scenario I and mandatory tests 6 and 9 on the real runner

| ID | Acceptance criterion |
|---|---|
| AC-19.10.1 | **Mandatory test 9.** On the real runner, an engineering employee's attempts to reach production are each refused and audited: `git push` to `main` from the session (hook denial; no credential), asking the controller to push to `main` or a tag (bridge denial), a push with the App token to `main` (ruleset rejection), merging its own pull request (no tool; API rejection), changing a workflow file (no `workflows` permission), and calling a deploy (no tool, no credential). Each attempt writes a `tool_invocations` or policy-decision record and an activity record, and Noa's supervision sees them. Phase 6 proved the policy with fakes; this is the real runner and the real GitHub. |
| AC-19.10.2 | **Scenario I** ([TESTING_STRATEGY §10](../../engineering/TESTING_STRATEGY.md#10-office-acceptance-scenarios)): an engineering employee's fix for a production problem cannot reach production without CI, permissions, pull request review and the owner's deployment gate. Every bypass attempt (pushing to `main`, merging its own pull request, skipping a failing test, widening its own grants, deploying directly, running a non-allowlisted remediation) is refused and recorded, and the legitimate path (request, branch, pull request, CI, review, staging, owner gate) succeeds. |
| AC-19.10.3 | **Mandatory test 6** passes end to end (proposal in Phase 14, pull request here, measurement after release), as in AC-19.7.2. |

## Test plan

Unit and policy tests for every engineering tool and the runner's options builder; hook tests with a corpus of denied commands and paths; contract tests for GitHub webhooks with recorded fixtures (redeliveries, out of order); integration tests of the runner against a sandbox repository with the same rulesets; CI tests for the extended integrity checks and the App manifest check; evaluation sets for Alex, Amir and Iris; prompt-injection corpus in issues, pull request comments, CI logs and code comments; mandatory tests 6 and 9 and Scenario I on the real repositories with sandbox branches.

**Mandatory tests (PLAN §5) owned here:** 6 (pull request by an engineering employee, with the equivalence test) and 9 (a GitHub agent tries to push to production: policy denial and audit, on the real runner).

## Security checklist

- [ ] GitHub App with the permissions listed in 19.2 only; no admin, no `workflows`, no secrets; installation tokens short-lived and requested per push by the controller.
- [ ] Rulesets protect `main`, release branches and tags; no bypass for the App or any employee.
- [ ] The runner holds no production credential; `env` checked in CI; the session never sees a GitHub token.
- [ ] Worktree confinement, command allowlist and network limits tested.
- [ ] Remediation allowlist reviewed by the owner; every remediation reversible and idempotent.
- [ ] Repository content, issues, comments, logs and traces treated as untrusted input; injection corpus passes.
- [ ] Separate kill switches for each engineering employee and for the runner, tested.
- [ ] THREAT_MODEL updated for self-modification risks.

## Documentation

`docs/features/engineering-requests/`, `docs/features/engineering-runner/`, `docs/features/employees/alex.md`, `amir.md`, `iris.md`; a new ADR for the runner's sandbox and GitHub isolation (or the ADR-0016 validation log); ENGINEERING_RULES updated for employee-authored pull requests; THREAT_MODEL; DATA_MODEL (`engineering_requests`, `engineering_changes`, `debug_reports` marked created); EVENT_CATALOG; runbook for suspending the App and the runner; TESTING_STRATEGY SCN-I status.

## Demo

Open an incident; Amir diagnoses it from traces and opens a request; his code step on the runner produces a branch with a regression test; the controller opens the pull request; CI runs; the owner reviews and merges; the release gate is approved from the Engineering Console with step-up. Then show Alex turning an approved Omer proposal into a workflow pull request with an equivalence test. Finally run the test-9 attempts live and show each denial in the audit trail.

## Exit gate

- [ ] Mandatory tests 6 and 9 and Scenario I pass on the real runner and repositories.
- [ ] At least one real request from each source kind handled end to end.
- [ ] Alex, Amir and Iris are `ready`, their autonomy approved by the owner, their routing measured.
- [ ] The owner approved moving on.

## Rollback

Kill switches per engineering employee and for the runner; the owner can suspend the GitHub App; every change already follows the normal revert and rollback path; open agent branches can be deleted without effect on `main`.

## Risks

| Risk | Mitigation |
|---|---|
| An employee finds a path around a gate | Gates enforced by GitHub rulesets, CI and the backend, not by prompts; several independent layers (hook, controller, bridge, token, ruleset); Scenario I and test 9 re-run at every release |
| GitHub cannot restrict pushes to `agent/**` as assumed | Verified at kickoff on a sandbox repository; fallback is a fork owned by the machine account with pull requests into the main repository |
| Sandbox escape or network exfiltration from the shell | Command allowlist, no secrets in the sandbox, egress limits or a no-network nested sandbox |
| Review load on the owner | Small pull requests; Noa samples engineering changes; low-risk changes batched |
| Engineering sessions are expensive (large context, many turns) | Per-request budgets reserved before the session; `max_turns`; Omer measures cost per request |

## Cost impact

Model usage of the three engineering employees, measured per request and capped by the engineering budget; one more DigitalOcean worker component for the runner (size verified at kickoff, see [COST_MODEL](../../cost/COST_MODEL.md)); the GitHub plan that supports rulesets on private repositories, if not already paid.
