# Glossary

Shared vocabulary for code, docs and conversations. When a term here has a code name, use exactly that name in code.

| Term | Code name | Meaning |
|---|---|---|
| Organization | `organization` | The business that owns shops and users. Every record belongs to one organization. |
| Shop | `shop` | One storefront on one marketplace (for example one Etsy shop). Every commercial record belongs to one shop. |
| Marketplace | `marketplace` | The platform a shop runs on: `etsy` now, `shopify` later. |
| Shop integration | `shop_integration` | The connection between a shop and its marketplace account: OAuth tokens (encrypted), scopes, verified capabilities, sync state. |
| Capability (integration) | `capability` | Something our app is verified to be allowed to do on a marketplace, for example `etsy.shipments.write`. Probed, never assumed. |
| Receipt | — | Etsy's name for an order. We store it as an `order` and keep the receipt ID as `external_id`. |
| Transaction | — | Etsy's name for one line of a receipt. We store it as an `order_item`. |
| Order | `order` | Our internal order record, one per marketplace receipt, unique by `shop_id + external_id`. |
| Product | `product` | Our internal product (what we sell). Not the same as a listing. |
| Variant | `variant` | A specific version of a product (size, colour), with its own SKU. |
| Listing | `listing` | A product as published in one shop, with its own title, price and marketplace ID. One product can have several listings. |
| Supplier mapping | `supplier_mapping` | The approved supplier offer used for a product or variant. Approved once, used for every future order. |
| Supplier quote | `supplier_quote` | An offer from a supplier for a product or variant, collected during onboarding. |
| Fulfillment | `fulfillment` | Getting an order from supplier to buyer. AutoVA executes it; we observe and verify it. |
| Tracking event | `tracking_event` | A recorded fact about shipment tracking (number appeared, carrier, status), with its source. |
| SLA window | `sla_window` | How long we wait for something (such as tracking) before it counts as late. Depends on shop, supplier and shipping method. |
| Incident | `incident` | A verified problem that needs handling, with its own state machine. Deduplicated per root cause and resource. |
| Remediation | `remediation` | A fix for an incident. Allowlisted remediations can run automatically; others need approval or a developer. |
| Task | `task` | A business unit of work with state, owner, SLA, idempotency key and trace. People, employees and automations all work through tasks. A finished conversation is not proof that a task completed. |
| Approval | `approval` | A request for a human decision on a risky action, with impact, risk, expiry and policy version. |
| Policy engine | `policy` | Backend component that decides if an actor may perform a capability on a resource. Deny by default. |
| Policy version | `policy_version` | Identifier of the policy rules in force when a decision was made, stored with every decision. |
| Actor | `actor` | Who performs an action: a `user`, an `employee`, an `automation`, an `advisor` or the `system`, each with its own identity. |
| AI employee | `employee` | A member of the AI workforce with a permanent identity (name, title, department) and a versioned profile. Not a prompt, not a process. Sixteen exist ([ADR-0012](../adr/0012-living-office-organization.md)). |
| Employee profile | `employee_profile_version` | The versioned configuration of an employee: responsibilities, tools, automations, routing policy, autonomy, readiness and the 22-section Agent Specification Sheet. Replaces version 1.0's "agent profile". |
| Agent Specification Sheet | — | The 22 sections every employee profile defines, from mission to performance metrics; sections still being designed are marked draft. |
| Readiness | `readiness` | `draft`, `specified`, `evaluating`, `ready`, `active`, `suspended`, `retired`. Only `ready` and `active` may work in production, and only after a passed evaluation. |
| Agent session | `agent_session` | One run of an employee on a task in an agent runtime, checkpointed so it can pause and resume. Replaces "agent run". |
| Automation | `automation` | A deterministic workflow of the Automation Department, triggered by an event, a schedule, an authorized employee, a user or another workflow; no model tokens unless a named step needs reasoning. |
| Automation execution | `automation_execution` | One run of an automation, unique per idempotency key. |
| Tool | `tool` | A versioned capability an employee may call if granted, executed by a use case after an access check. Tools never decide objectives. |
| Decision Advisor | `decision_advisor` | A shared structured-decision service (Jev) behind a provider-independent port; rules decide first; it never authorizes. |
| Routing policy | `routing_policy` | An employee's versioned rules for model use: providers, capabilities, tiers, budget, data class, fallbacks, advisor use. One central router applies them all. |
| Activity record | `activity_record` | The record of one meaningful operation by any actor, with organization, shop, task, correlation and outcome; never private model reasoning. |
| Quality finding | `quality_finding` | A problem found by a monitor or by Noa, with evidence and a follow-up that is verified before it closes. |
| Optimization proposal | `optimization_proposal` | Omer's structured proposal with baseline, evidence, acceptance criteria and rollback, followed from detection to verified result. |
| Engineering request | `engineering_request` | A piece of engineering work linked to its source and to issues, pull requests, tests and releases. |
| Side-effect level | `side_effect_level` | How risky a tool is: `none`, `internal` (reversible inside HTCO), `external` (changes another system), `financial`. |
| Autonomy level | `autonomy_level` | L0 read-only and suggestions, L1 reversible internal actions, L2 external actions after approval, L3 external actions within a pre-approved policy. |
| Model gateway | `model_gateway` | The single path to any AI model provider. Applies routing, budgets, masking and logging. |
| Model catalog | `model_catalog` | Verified list of provider models with IDs, prices (as-of date), capabilities and eval scores. |
| Routing decision | `routing_decision` | Record of which route or model was chosen for a task, why, at what cost, and the quality outcome. |
| Budget ledger | `budget_ledger` | Reserved and actual spend per organization, shop, employee, task, day and month. |
| Checkpoint | — | Saved state of an agent run (LangGraph) used to resume after a pause, crash or approval. |
| Inbox / outbox | `inbox`, `outbox` | Tables that make external events idempotent on the way in (inbox) and side effects reliable on the way out (outbox). |
| Idempotency key | `idempotency_key` | A deterministic key that makes repeating an operation safe, for example `etsy:{shop}:{receipt}:paid`. |
| Correlation ID | `correlation_id` | ID that ties together everything caused by one trigger, across API, queue, automations and employees. |
| Trace ID | `trace_id` | OpenTelemetry trace identifier for one request or job execution. |
| Dead-letter queue | DLQ | Where work goes after its retries are exhausted, for a person or Amir. |
| Lease | `lease` | Time-limited claim on a task by one worker; renewed by heartbeat; expires if the worker dies. |
| HTCO Command | — | The dashboard and CRM product surface (web, Windows, Android). |
| HTCO Office | — | The organization at work in HTCO Command: Executive Office, departments, employee profiles, Automation Center, AI Tools Center, Engineering Console, communication, observatory and activity feed, built only from recorded activity. |
| Owner Assistant | — | The owner's conversational interface to the office. |
| Listing Health | — | The first AI workflow: auditing existing listings for accuracy and proposing fixes. |
| Product Studio | — | The AI workflow that turns a product idea into a policy-compliant listing. |
| ESTIMATED / VERIFIED | `estimated`, `verified` | Labels on financial figures: estimated from partial data, or verified against the source of truth. |
| ADR | — | Architecture Decision Record, stored in `docs/adr/`. |
| Slice | — | A small vertical increment inside a phase, delivered as one pull request with its own tests. |
