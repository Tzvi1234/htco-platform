# Permission Matrix

| | |
|---|---|
| **Status** | 2.0 — the living office ([ADR-0012](../adr/0012-living-office-organization.md)). Section 1 is enforced in code by policy engine v0 (`htco_application.access.policy`, slice 2.4), whose test parses this table so the two cannot drift; RLS adds tenant isolation in the database (Phase 2). Employees' tool and automation grants (section 3) are enforced by `htco_application.workforce.tool_access` and the automation engine, from `registry/`; automations (section 2) from Phase 5. Extended per phase. |
| **Principle** | Deny by default. A permission not listed here does not exist. |

Legend: **✓** allowed · **S** allowed for shops granted to the user · **MFA** requires an `aal2` session · **RA** requires recent re-authentication (step-up, default 10 minutes) · **A** creates an approval instead of acting · **—** denied.

Owner and Admin have organization-wide access to every shop; all other roles see only the shops granted to them.

## 1. Human roles

| Capability | Owner | Admin | Operations | Finance | Support | Developer | Viewer |
|---|---|---|---|---|---|---|---|
| View overview and KPIs | ✓ | ✓ | S | S | S | S | S |
| Manage users, roles, shop access | ✓ MFA | ✓ MFA | — | — | — | — | — |
| Connect or disconnect a shop | ✓ MFA RA | — | — | — | — | — | — |
| View orders and fulfillment | ✓ | ✓ | S | S | S | S | S (masked) |
| View buyer contact details | ✓ | ✓ | S | — | S | — | — |
| View incidents | ✓ | ✓ | S | S | S | S | S |
| Open or update incidents | ✓ | ✓ | S | — | S | S | — |
| Retry allowlisted sync | ✓ | ✓ | S | — | — | S | — |
| Confirm a shipment write to Etsy | ✓ MFA RA | ✓ MFA RA | S A | — | — | — | — |
| Request a bulk re-sync | ✓ | ✓ | S A | — | — | S A | — |
| View tasks and approvals | ✓ | ✓ | S | S | S | S | S |
| Decide low-risk approvals | ✓ MFA | ✓ MFA | S MFA | S MFA | — | — | — |
| Decide high-risk approvals (money, publishing, permissions) | ✓ MFA RA | — | — | — | — | — | — |
| View finance and export CSV | ✓ MFA | ✓ MFA | — | S MFA | — | — | — |
| Edit model policies, budgets, kill switches | ✓ MFA RA | — | — | — | — | — | — |
| Enter or edit product facts | ✓ | ✓ | S | — | — | — | — |
| Run a Listing Health audit | ✓ MFA | ✓ MFA | S | — | — | — | — |
| View the office: employees, automations, tools and costs | ✓ | ✓ | S | S | — | S | — |
| View agent sessions, routing decisions and traces | ✓ | ✓ | S | — | — | S | — |
| View audit log | ✓ | ✓ | — | — | — | S | — |

While the organization has a single owner, high-risk approvals rely on step-up re-authentication; a two-person rule (requester plus a different approver, or two approvers) becomes a policy option once a second Admin exists.

## 1a. Planned human capabilities (2026-10-23, not yet enforced)

These rows come from [ADR-0020](../adr/0020-per-shop-browser-environment.md) to [ADR-0022](../adr/0022-etsy-write-tools-and-browser-scope.md). Each moves into section 1, with its `Capability` and its test, in the phase that builds it (6–7 Etsy writes and approvals, 8A browser environments, 8B authenticator).

| Planned capability | Owner | Admin | Operations | Finance | Support | Developer | Viewer |
|---|---|---|---|---|---|---|---|
| Run an Etsy write tool by hand (first run, owner-trigger; ADR-0022) | ✓ MFA RA | — | — | — | — | — | — |
| Decide Etsy write approvals W1 | ✓ MFA | ✓ MFA | — | — | — | — | — |
| Decide Etsy write approvals W2 | ✓ MFA RA | ✓ MFA RA | — | — | — | — | — |
| Decide W3 approvals: listing deletion, activation with fee, money actions | ✓ MFA RA | — | — | — | — | — | — |
| Create, suspend, resume or delete a Browser Environment | ✓ MFA RA | ✓ MFA RA | — | — | — | — | — |
| Sign in through the live view of a Browser Environment | ✓ MFA RA | ✓ MFA RA | — | — | — | — | — |
| Switch a browser scope flag on (only when its scope row is recorded) | ✓ MFA RA | — | — | — | — | — | — |
| Enrol, rotate or remove a TOTP account | ✓ MFA RA | ✓ MFA RA | — | — | — | — | — |
| View a TOTP code (`view_totp_code`, per shop) | ✓ MFA RA | S MFA RA | — | — | — | — | — |

## 2. Automations

Automations run under their own service identities with least-privilege database grants ([ADR-0013](../adr/0013-automation-engine.md)); executions run in the `agent-worker`'s `htco_worker` role, one organization per job (slice 5.1). A user may start an automation by hand only when its registry definition names a `user_capability` from section 1 and the user's role holds it; listing automations and executions is "View the office"; retrying a dead-lettered execution is an organization-wide "Retry allowlisted sync" (Owner, Admin), audited (slice 5.10). The full list and each one's permissions section are in [`registry/automations/`](../../registry/automations).

| Automation | Allowed | Not allowed |
|---|---|---|
| Etsy Order Intake, Order Deduplication & Verification | Read Etsy receipts; upsert orders, items, shipments; write inbox, events and activity records | Any Etsy write; reading tokens of other shops |
| Product Sales Counters, Finance Ledger Ingestion | Write counters and ledger entries from verified events | Changing orders; any external call |
| Tracking Synchronization, Incident Detection & Escalation | Read orders and shipments; create and close incidents; request notifications | Etsy writes |
| Catalog Draft Creation & Publishing | Create drafts; publish only an approved specification | Pricing changes; publishing without an approval |
| Supplier Quote Request & Follow-up | Record requests and quotes; update the workflow status | Activating a mapping without approval; payments |
| Order & Financial Reconciliation | Read internal and external records; write reconciliation evidence and findings | Correcting records directly |
| Outbox delivery (platform) | Execute outbox items that are approved or policy-allowed | Creating outbox items |
| Token refresh (platform) | Refresh and store tokens for one shop at a time | Using tokens for anything else |

## 3. AI employees (initial grants)

Autonomy levels: L0 suggest only · L1 reversible internal actions · L2 external actions after approval · L3 pre-approved external actions within a policy. The source of truth is each employee's `allowed_tools` and `allowed_automations` in [`registry/employees/`](../../registry/employees), enforced by `decide_tool_access` and the automation engine ([ADR-0014](../adr/0014-tool-registry.md)); this table summarizes them. Every grant is a draft until the employee's activation phase confirms it.

| Employee | Level | Key tools and automations | Requires approval | Denied |
|---|---|---|---|---|
| Maya | L1 | `tasks.*`, `activity.query`, `approvals.request`, messages; Task Scheduling & Recovery | Changing policies, budgets or autonomy | Department work on others' behalf; approving her own requests |
| Noa | L1 | `activity.query`, `quality.record_finding`, `incidents.open`, read-only orders and logs; Reconciliation, Quality Review Scheduling | — (records findings and tasks only) | Repairing systems; closing findings without evidence |
| Omer | L0 | `metrics.query_costs`, `optimization.propose`, `engineering.create_request` | — (only proposes) | Implementing, deploying, changing routing policies |
| Lia | L1 | `tasks.*`, `approvals.read`, messages | — | Deciding or approving for the owner |
| Shon | L0 | `research.*`, `suppliers.search`, `decisions.advise` | — (only proposes) | Publishing, pricing, contacting suppliers |
| Noam | L1 | `catalog.read`, `catalog.update_draft_content` | Applying copy to a live listing | Publishing, pricing |
| Nika | L1 | `media.*` | Using media on a live listing | Assets without rights |
| Daniel | L1 | `catalog.create_draft`, `pricing.calculate`, `automation.trigger`; Catalog Draft Creation & Publishing | Publishing (through the automation) | Money arithmetic in a model |
| Tom | L2 | `suppliers.*`, `pricing.calculate`; Supplier Quote Request & Follow-up | Activating a supplier mapping | Payments, orders |
| Roni | L0 | `finance.read_ledger`, `metrics.query_costs`; Reconciliation | — | Writing ledger entries, payments |
| Eitan | L1 | `orders.read`, `incidents.open`; Order Intake, Deduplication, Tracking Sync | — | Etsy writes, buyer contact |
| Gal | L0 | `listings.read_stats`, `catalog.read`, `orders.read` | — (only proposes) | Buyer marketing lists, ad buying |
| Dana | L2 | `orders.read`, `email.draft_reply`, `email.send` | Sending any reply | Refunds, promises, marketing |
| Alex | L1 | `github.*` (read, issues, pull requests), `engineering.create_request` | Merging and releasing (CI, review, owner gate) | Weakening tests, removing logging, widening permissions, bypassing branch protection |
| Amir | L1 | Logs, traces, `incidents.open`, `remediation.run_allowlisted`, `github.*`; Task Scheduling & Recovery | Any non-allowlisted remediation | Money, permissions, production code without review, customer contact |
| Iris | L1 | `github.*`, `registry.read`, `evaluations.run` | Changing any employee's permissions, budget or autonomy | Deploying AI-generated changes to production |
| Jev (advisor) | — | Answers typed questions | — | Authorizing anything |

Every employee: no access to tokens, other tenants, the audit log, policies, budgets or other employees' memory. A tool call outside its grants is denied and emits `agents.tool_denied.v1`.
