# HTCO OS — Spec Sheet

| | |
|---|---|
| **Product** | HTCO OS (HT Commerce), by H.T.Z Studios |
| **Edition** | Claude-Native Hybrid Edition (owner's master plan v6.0, 14 October 2026) |
| **Status** | Specification; nothing here is deployed yet unless the [plan status](../plan/PLAN.md#8-status) says so |
| **Last updated** | 2026-10-14 |

The whole system on one page: what it is, what it is made of, who works in it, and where to read more. If this sheet and a detailed document disagree, the detailed document wins and this sheet is fixed in the same pull request. Sources: [PRD](PRD.md) · [PLAN](../plan/PLAN.md) · [Software design](../architecture/SOFTWARE_DESIGN.md) · [Code design](../architecture/CODE_DESIGN.md) · [ADRs](../adr/README.md).

## 1. In one paragraph

One control center for one or more Etsy shops connected to AutoVA. It records real orders, fulfillment, revenue and profit, and runs a **living office**: 16 AI employees in seven departments who research, write, check, supervise and improve under written policies and the owner's approvals. Repeatable work is done by deterministic automations, not by AI. Every action leaves a record, and the screens show only what the records say.

## 2. System at a glance

| Area | Specification |
|---|---|
| Repositories | `htco-platform` (backend, automations, Claude-native agent runtime, model routing, integrations, Supabase and DigitalOcean infrastructure, system docs) and `htco-clients` (web dashboard, Windows and Android apps, office visualizer, design system, typed API clients). Both private. |
| Backend | Python 3.14, FastAPI, uv workspace, psycopg with explicit SQL; modular monolith with hexagonal layers ([ADR-0002](../adr/0002-modular-monolith-hexagonal.md)) |
| Agent runtime | Claude Agent SDK behind the `AgentRuntime` port (default); LangGraph as the alternative adapter ([ADR-0016](../adr/0016-claude-native-agent-runtime.md)) |
| Model access | Own model gateway and router with a policy per employee; Anthropic models in the Claude runtime; OpenAI Images through Nika's tools ([ADR-0018](../adr/0018-image-generation-specialist.md)); Jev as Decision Advisor after a benchmark ([ADR-0015](../adr/0015-decision-advisor-and-routing.md)) |
| Billing of AI | `API_PRODUCTION` (service-account API key) for every hosted run; personal subscriptions for developers' own sessions only; `APPROVED_SUBSCRIPTION_RUNTIME` off ([ADR-0017](../adr/0017-auth-and-billing-policy.md)) |
| Database | Supabase Pro Postgres with RLS on every table; Auth with MFA; Storage for media; queues chosen by spike ([ADR-0007](../adr/0007-job-queue.md)) |
| Hosting | DigitalOcean App Platform, Frankfurt; apps `htco-dev` (pilot) then `htco-prod`; components `api` (web service), `web` (static site or small service), `agent-worker` (worker); `automation-worker` and `browser-runner` only by ADR |
| Clients | Web: React, TypeScript, Vite. Windows: C#, WinUI 3. Android: Kotlin, Jetpack Compose. One design system, generated API clients |
| Domains | `app.ht-co.app` (web, office at `/office`), `api.ht-co.app` (native clients, webhooks); `office.ht-co.app` and `agents.ht-co.app` are mail-only (MX, SPF, DKIM, DMARC) |
| Mail | `support@`, `info@`, `noreply@office.ht-co.app` through one managed provider; later `<employee>@agents.ht-co.app` as virtual aliases |
| Languages | Hebrew (RTL) and English (LTR) in every client; repository content in English |
| Security baseline | OWASP ASVS 5.0 Level 2 plus agentic controls (OWASP Top 10 for Agentic Applications 2026); [threat model](../architecture/THREAT_MODEL.md) |

## 3. The office

**Owner → Maya (General Manager) → departments.** Noa and Omer review across all departments and may propose stopping or improving work through the policy engine. Engineering serves every department. Automations, AI Tools and Infrastructure are **service departments, not people**. Jev is a shared Decision Advisor, never an employee.

| Employee | Title | Department | Can (capabilities) | Never | Activated in |
|---|---|---|---|---|---|
| Maya | General Manager | Executive Office | `task.create`, `task.delegate`, `task.read`, `approval.request`, `report.read` | Bypass approval gates; edit the ledger | 7 |
| Noa | Quality & Operations Supervisor | Executive Office | `audit.read`, `quality.finding.create`, `incident.create`, `task.reassign.request` | Declare success without source evidence; fix money | 7 (pilot), 14 (full) |
| Omer | Continuous Optimization Officer | Executive Office | `metrics.read`, `optimization.propose`, `eval.request`, `issue.create` | Change a model, budget or production without policy and approval | 7 (pilot), 14 (full) |
| Lia | Owner Assistant | Executive Office | `owner.request`, `report.read`, `approvals.read`, `notifications.send` | Gain admin rights from her name | 15 |
| Shon | Product Intelligence | Product Studio | `web.research`, `suppliers.search`, `products.propose`, `decision.classify` | Publish products; approve Etsy prohibitions | 7 (research), 12 (full) |
| Noam | SEO & Copywriting | Product Studio | `catalog.read`, `content.write.draft`, `seo.check` | Invent claims; publish | 11 |
| Nika | Creative & GPT Image Specialist | Product Studio | `media.generate_gpt`, `media.edit`, `media.validate`, `assets.write` | Misrepresent a product; release an image without a rights check | 12 |
| Daniel | Catalog & Pricing Architect | Product Studio | `catalog.compose`, `pricing.calculate`, `listing.submit_for_review` | Publish through the API himself; calculate money in a model | 11 |
| Tom | Procurement & Supplier Quotes | Procurement & Finance | `quotes.request`, `quotes.review`, `supplier_mapping.propose` | Re-choose the supplier for every order | 13 |
| Roni | Finance Controller | Procurement & Finance | `finance.read`, `ledger.reconcile`, `finance.report` | Record transactions outside the ledger engine | 13 |
| Eitan | Fulfillment Inspector | Order Assurance | `orders.read`, `shipments.read`, `incident.create` | Process every order by hand | 10 |
| Gal | Marketing & Growth | Marketing | `metrics.read`, `marketing.propose`, `content.request` | Contact Etsy buyers through an unauthorized channel | 21 |
| Dana | Customer Service | Customer Support | `tickets.read`, `tickets.reply_draft`, `orders.read_limited` | Refund or make an unusual commitment without approval | 15 |
| Alex | Lead Software & Platform Engineer | Engineering | `github.branch`, `github.pr`, `ci.read`, `engineering.develop_sandbox` | Deploy to production freely | 19 |
| Amir | Debugger & Reliability Engineer | Engineering | `logs.read`, `incident.triage`, `remediation.safe`, `github.pr` | Disable tests or change production without approval | 19 |
| Iris | Agent Systems & Experience Engineer | Engineering | `agent_profile.propose`, `eval.run`, `tool.build`, `design.pr` | Change permissions or production models without a gate | 19 |

Capabilities are the master plan's names. They map to registered tools in [`registry/tools/`](../../registry/tools) when each employee is specified; the registry is the source of truth. Nika's registry title is still "Creative & Media"; it is renamed when her sheet is completed in Phase 12.

Every employee has a versioned profile with: mission, business goal, forbidden actions, activation events, input and output schemas, allowed tools, workflows and shops, skills, knowledge sources, memory policy, communication roles, routing and decision policies, maximum task cost, risk tier, approval matrix, timeouts, retries, evaluation suite, deployment state, created by and reviewed by.

**Real statuses** shown in the office: `IDLE`, `QUEUED`, `RUNNING`, `WAITING_EXTERNAL`, `WAITING_APPROVAL`, `BLOCKED`, `FAILED`, `OFFLINE`. They come from task runs and the state machine, never from animation.

## 4. Automations (no AI in the normal path)

| Master plan workflow | Registry automation | Trigger | Business owner | Phase |
|---|---|---|---|---|
| `etsy.order_intake` | `etsy-order-intake`, `order-dedup-verification` | Webhook | Eitan, Finance | 4 (fixtures), 9 (live) |
| `orders.sales_counters` | `product-sales-counters` | Event | Roni | 4 |
| `agent.task_dispatch` | `agent-task-scheduling-recovery` | Queue, event | Maya, system | 5 |
| `notifications.report` | `scheduled-reporting` | Event, schedule | Lia | 5 |
| `quality.nightly_audit` | `quality-review-scheduling` | Schedule | Noa | 7 |
| `optimization.periodic` | `optimization-review-scheduling` | Schedule, metric | Omer | 7 |
| `tracking.sync` | `tracking-sync` | Event, schedule | Eitan | 10 |
| `incident.detect_escalate` | `incident-detection-escalation` | Event, metric | Noa, Amir | 10 |
| `autova.fulfillment_watch` | `autova-fulfillment-watch` (to add) | Event, schedule | Eitan | 10 |
| `catalog.publish` | `catalog-draft-publishing` | Authorized task | Daniel, Noa | 12 |
| `quotes.initial_request` | `supplier-quote-followup` | Event | Tom | 13 |
| `finance.ledger_ingest` | `finance-ledger-ingestion` | Event | Roni | 13 |
| `inventory.price_watch` | `inventory-price-monitoring` | Schedule | Tom, Daniel | 13 |
| `reconciliation.daily` | `order-financial-reconciliation` | Schedule | Noa, Roni | 13 |
| `product.metrics_rollup` | `product-metrics-rollup` (to add) | Schedule | Gal, Roni | 14 |
| `mail.inbound_route` | `email-intake-routing` | Webhook | Dana | 15 |
| `agent.mail_alias_routing` | `agent-mail-alias-routing` (to add) | Webhook | Lia | 15 |

Every workflow has a trigger, owner, typed input and output, precondition, approval, idempotency, retry and backoff, timeout, compensation, audit, version, tests and metrics. Agents reach workflows only through `automation.trigger` and the policy gate.

**Commerce rules.** AutoVA keeps doing fulfillment after an approved supplier mapping per product or variant; the supplier is not re-chosen per order. Tracking is written to Etsy only with proven permission; otherwise through the authorized AutoVA path. Etsy's dropshipping and reselling policy is checked before any product is made or published. Provenance, IP, taxes and customer consent are recorded.

## 5. Tools

Tools are capabilities, never employees. Families: Commerce, Supplier, Research, Image, Finance, Communication, GitHub, Infrastructure, Decision, Automation, Analytics. Each tool has an owner, version, typed schemas, required capabilities, shop scope, permission policy, idempotency, audit, quotas, tests and cost. Email, web pages and supplier pages are untrusted input. An MCP connection grants no authority by itself: the backend authorizes every action ([ADR-0014](../adr/0014-tool-registry.md)).

## 6. Model routing

| | |
|---|---|
| Parts | `TaskRouter` (which employee or workflow), `ModelGateway` (which model), `DecisionAdvisor` (structured decisions), `CostLedger` (budgets), `QualityGate` (result checks) |
| Inputs | employee, organization and shop, task capability, risk, data class, required modalities and tools, estimated context, cost and latency budgets, permitted providers, quota state, policy version, evaluation scorecard |
| Outputs | provider, model reference, billing mode, decision reason code, fallback chain, approval required, budget reserved, policy version |
| Order | (1) no AI if possible, (2) security gate, (3) capability fit, (4) quality benchmark, (5) best cost and latency, (6) fallback on error, (7) final quality check |
| Budgets | per organization, shop, employee and task, daily and monthly; alerts at 70, 90 and 100%; `cost_per_listing`, `cost_per_order`, `cost_per_ticket`, `cost_per_image`, `cost_per_incident` |
| Never | hard-coded model IDs or prices; invented subscription economics; advisors in authorization decisions |

## 7. Data (pilot tables first)

`organizations`, `shops`, `users`, `shop_memberships`, `employee_identities`, `employee_profile_versions`, `business_tasks`, `task_executions`, `employee_messages`, `business_events`, `audit_events`, `approvals`, `automation_definitions`, `automation_executions`, `model_catalog`, `routing_policies`, `routing_decisions`, `model_usage` (the master plan's `model_usage`), `quality_findings`, `optimization_proposals`, `incidents`, plus `agent_runs` and `tool_invocations`. Commerce tables (products, orders, procurement, finance) arrive in their phases. Every business row carries `organization_id`; commercial rows also `shop_id`; activity rows carry `actor_id`, `task_id`, `correlation_id`, `policy_version`, timestamp and outcome. Details: [DATA_MODEL.md](../architecture/DATA_MODEL.md).

## 8. Screens

43 screens in the clients specification, from sign-in to the office, centers and consoles, each with loading, empty, error, permission, stale and offline states. Platform priority per screen: all platforms, or web and Windows only. The matrix lives in `htco-clients/docs/ux/SCREEN_SPEC.md`; the navigation in `htco-clients/docs/ux/NAVIGATION_MAP.md`.

## 9. Approvals and autonomy

- **Autonomous:** research, drafts, classification, reports, notifications, safe retries, sandbox tests.
- **Needs approval:** supplier purchases, refunds, first product or a new category, sensitive email, permission changes, price changes below the floor, package installs, production deploys, customer marketing.
- High-risk approvals need recent re-authentication. Nobody approves their own request.

## 10. Quality bar

- Red lines: [ENGINEERING_RULES §0](../engineering/ENGINEERING_RULES.md#0-red-lines).
- Tests first, with coverage floors (domain 95%, application 90%) and test-integrity checks: [TESTING_STRATEGY](../engineering/TESTING_STRATEGY.md).
- Fifteen mandatory business, AI and DevOps tests: [PLAN §5](../plan/PLAN.md#5-mandatory-tests-of-the-master-plan).
- No employee or automation is active without a complete specification, a passed evaluation and the owner's approval.

## 11. Milestones

M0 Audit and plan (phase 0) · M1 Foundation (5) · M2 Workforce pilot (7) · M3 Models and tools (8) · M4 Commerce (13) · M5 Governance (14) · M6 Clients (18) · M7 Release (20). Details and status: [PLAN](../plan/PLAN.md).
