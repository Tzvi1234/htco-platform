# Permission Matrix

| | |
|---|---|
| **Status** | 1.0 — section 1 is enforced in code by policy engine v0 (`htco_application.access.policy`, slice 2.4), whose test parses this table so the two cannot drift; RLS adds tenant isolation in the database (Phase 2); workers and agents are enforced from Phases 5 and 8. Extended per phase. |
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
| View agent directory and costs | ✓ | ✓ | S | S | — | S | — |
| View agent runs and traces | ✓ | ✓ | S | — | — | S | — |
| View audit log | ✓ | ✓ | — | — | — | S | — |

While the organization has a single owner, high-risk approvals rely on step-up re-authentication; a two-person rule (requester plus a different approver, or two approvers) becomes a policy option once a second Admin exists.

## 2. Workers

| Worker | Allowed | Not allowed |
|---|---|---|
| Ingest | Read Etsy receipts; upsert orders, items, shipments; write inbox and events | Any Etsy write; reading tokens of other shops |
| Tracking and SLA inspector | Read orders and shipments; create and close incidents; request notifications | Etsy writes |
| Outbox delivery | Execute outbox items that are approved or policy-allowed | Creating outbox items |
| Token refresh | Refresh and store tokens for one shop at a time | Using tokens for anything else |

## 3. Agents (initial profiles)

Autonomy levels: L0 suggest only · L1 reversible internal actions · L2 external actions after approval · L3 pre-approved external actions within a policy.

| Agent | Level at start | Allowed tools | Requires approval | Denied |
|---|---|---|---|---|
| Quality Supervisor (Listing Health) | L0 | `listing.read`, `product_facts.read`, `listing.propose_fix` | — (only proposes) | `listing.update`, any external write |
| SEO Copywriter | L1 | `listing.read`, `listing.draft_copy` | Applying copy to a live listing | Publishing, pricing |
| Debugger | L1 | `incidents.read`, `orders.read`, `sync.retry` (allowlisted), `incidents.propose_fix` | Any non-allowlisted remediation | Money, permissions, code, customer contact |
| General Manager | L1 | `tasks.create`, `tasks.assign`, `reports.read` | Changing policies | Direct tool calls on behalf of other agents |
| Publishing Agent (Phase 10) | L2 | `catalog.create_draft`, `listing.upload_image` | `listing.publish` | Pricing changes without the Pricing Analyst |
| Supplier Quote Agent (Phase 11) | L2 | `sourcing.read_quotes`, `sourcing.propose_mapping` | `sourcing.activate_mapping` | Payments |
| Customer Service (Phase 12) | L2 | `support.read_ticket`, `support.draft_reply` | Sending any reply | Refunds, marketing |

Every agent: no access to tokens, other tenants, the audit log, policies, budgets or other agents' memory. A tool call outside this table is denied and emits `agents.tool_denied.v1`.
