# HTCO OS — Product Requirements Document

| | |
|---|---|
| **Product** | HTCO OS (HT Commerce) — by H.T.Z Studios |
| **Status** | Draft 2.0 — the living office ([ADR-0012](../adr/0012-living-office-organization.md)); awaiting owner approval. Version 1.0's 18 agent roles are superseded (§8). |
| **Last updated** | 2026-10-14 |
| **Owner** | H.T.Z Studios |
| **Related** | [MVP](MVP.md) · [Master plan](../plan/PLAN.md) · [Software design](../architecture/SOFTWARE_DESIGN.md) · [Glossary](GLOSSARY.md) |
| **Sources** | The original HTCO planning brief v5 (2026-10-14), kept outside the repository. Where they differ, this document wins. |

This is the product specification for the whole system: what HTCO OS is, who it serves, what it must do, and how we know it works. Every functional requirement has an ID (`FR-…`) that phase plans, feature docs and tests reference, so any line of code can be traced back to a requirement here.

---

## 1. Summary

HTCO OS is an operating system for an online commerce business that runs on marketplaces, starting with Etsy. It combines three things that usually live in separate tools:

1. **A trustworthy operational core.** Orders, fulfillment, tracking, incidents, finance and audit, synchronized from the marketplace and computed in deterministic code.
2. **A living office.** Sixteen AI employees in seven departments, each with a permanent identity and a versioned profile; an Automation Department of deterministic workflows; an AI Tools Department of versioned capabilities; and an Engineering Department that maintains and improves the office itself. A shared Decision Advisor (Jev) helps with structured decisions. Employees wake on tasks, events and schedules, use only the tools they are granted, and ask a human before anything risky; an independent supervisor verifies their work and an optimizer makes it cheaper (§8).
3. **One command surface.** *HTCO Command* (web, Windows and Android apps) where the owner sees verified business state, approves actions, talks to employees, and watches the *HTCO Office*: departments, employees, automations, tools and engineering, shown only from recorded activity.

The product's promise is **autonomy you can audit**: every number shows its source and freshness, every agent action has a receipt, and every action that spends money or changes something outside the system needs the right approval.

## 2. Problem

A small team running one or more marketplace shops repeats the same work by hand and finds out about problems late:

- **Visibility is fragmented.** Orders live in Etsy, fulfillment and supplier data in AutoVA, costs in spreadsheets. Nobody sees one verified picture of an order from payment to delivery, or of what each sale actually earned.
- **Problems are discovered by customers.** A missing tracking number, a late shipment or a listing that over-promises (size, material, what is included) turns into a bad review or a refund before the owner notices. Public reviews of the pilot shop already show this pattern: "item not as described" complaints about size and material, and slow delivery.
- **Growth means more manual work.** Researching products, writing listings, preparing images, pricing, collecting supplier quotes and answering buyers all scale linearly with the catalog.
- **AI tools are either toys or risks.** Generic AI assistants can write copy, but they cannot be trusted to act: they have no shop isolation, no budget, no approvals and no audit trail.

## 3. Goals and non-goals

### Goals

| ID | Goal | Measured by |
|---|---|---|
| G1 | One verified operational picture per shop: orders, fulfillment state, incidents, profit | Dashboard data shows source and freshness, and matches the marketplace within the sync window |
| G2 | Catch fulfillment problems before customers do | Share of late or missing tracking detected before the buyer contacts the shop |
| G3 | Listings that tell the truth | Drop in "not as described" reviews and refunds after Listing Health ships |
| G4 | Delegate repeatable work to agents safely | Tasks completed by agents with no policy violations and no unapproved side effects |
| G5 | Know the cost of every unit of work | AI and infrastructure cost attributable per listing, order, ticket, agent and shop |
| G6 | Add a shop or marketplace without cloning the system | New shop connects through an adapter, with no new services or agents |
| G7 | An office that supervises and improves itself | Share of critical activity that is observable (target 100%); verified optimization savings per quarter; no improvement deployed without review and the release gate |

### Non-goals (v1)

- Replacing AutoVA's supplier ordering and payment. AutoVA stays the fulfillment executor (see §7.10).
- Running our own mail servers, or a mailbox per agent.
- A general-purpose chatbot. Agents act only through the tools and policies defined here.
- Marketing to Etsy buyers outside the channels Etsy allows.
- Public SaaS: multi-organization support exists in the data model from day one, but self-serve sign-up and billing for other businesses are out of scope for v1.

## 4. Users

### 4.1 Human personas

| Persona | Needs | Primary client |
|---|---|---|
| **Owner** | KPIs, approvals for spending and publishing, talking to the office manager, profit per shop | Android (on the go), Web, Windows |
| **Operations** | Order exceptions, tracking and SLA breaches, AutoVA mapping issues | Web, Windows |
| **Finance** | Revenue, fees, costs, margins, supplier quotes, data reconciliation | Web, Windows |
| **Support** | Tickets and order lookups, only for shops they are allowed to see | Web, Android |
| **Developer** | Incidents, agent runs, traces, safe remediation in staging | Windows, Web |
| **Viewer** | Read-only reporting | Web |

### 4.2 System actors

- **AI employees.** Sixteen employees (§8), each a permanent identity plus a versioned profile (responsibilities, tools, automations, routing policy, autonomy, memory, evaluations), not a running process. Employees never hold user credentials; they act through a service identity that the policy engine checks on every tool call.
- **Automations.** Deterministic workflows of the Automation Department (order intake, tracking sync, reconciliation, reports…) that call no model unless a named step needs reasoning ([ADR-0013](../adr/0013-automation-engine.md)).
- **The Decision Advisor.** A shared structured-decision service (Jev, behind a provider-independent port); it advises and never authorizes ([ADR-0015](../adr/0015-decision-advisor-and-routing.md)).
- **System.** Schedulers, monitors and the API itself. Every record names its actor as one of: user, employee, automation, advisor, system.
- **External systems.** Etsy Open API, AutoVA, the email provider, model providers (Anthropic, OpenAI and others), Supabase, DigitalOcean.

## 5. Product principles

1. **Status fidelity.** The UI reports verified backend state, never an agent's guess. Stale data is labeled as stale.
2. **Zero-AI first.** If code can do it deterministically (sync, math, validation, routing by rules), code does it. AI is used where judgment or language is needed.
3. **Human clarity on risk.** Any action that spends money, publishes, contacts a customer or changes permissions shows its impact, risk level and who approves it.
4. **Shop isolation by construction.** Every business record belongs to exactly one organization and shop. Isolation is enforced in the database and in the API, not by convention.
5. **Receipts for everything.** Every agent action and every approval produces an auditable record: what was requested, which tool ran, what changed, the before and after.
6. **Honest data.** Show `last updated`, the source system, and pending or failed states. Never show an endless spinner or invented activity.
7. **Native on each platform.** Shared design tokens and API contracts; native UI on Windows (WinUI 3), Android (Jetpack Compose) and the web.
8. **Accessible and bilingual.** Hebrew (RTL) and English (LTR), light and dark, keyboard and screen-reader support from the first screen.

## 6. Product surfaces

| Surface | What it is | Where |
|---|---|---|
| **HTCO Command** | The dashboard and CRM: overview, shops, orders, products, tasks, approvals, finance, support, incidents, administration | `dash.ht-co.app` (web), Windows app, Android app |
| **HTCO Office** | The organization at work: Executive Office, departments, employee profiles, Automation Center, AI Tools Center, Engineering Console, Office Communication, Decision & Model Observatory, Activity Feed & Audit, Notifications & Approvals (§7.16) | `dash.ht-co.app/office` and inside the native apps |
| **Owner Assistant (Lia)** | Conversations with Lia, Maya, one employee or a department; approvals inline | Inside HTCO Command; optional bridge to Hermes (WhatsApp/Telegram) through the Action API |
| **HTCO API** | The only door into the system for every client | `api.ht-co.app` |
| **Brand site** | Public marketing site | `ht-co.app` |
| **Email** | System mail and shared inboxes | `@office.ht-co.app` (support, info, noreply); later `@agents.ht-co.app` aliases |

## 7. Functional requirements

Priority says how essential a requirement is to its own capability: **P0** must hold before that capability ships, **P1** should ship with it or soon after, **P2** comes later. *When* a capability ships is set by the phase column and the [plan](../plan/PLAN.md); what the MVP contains is defined in [MVP.md](MVP.md).

### 7.1 Identity and access (IAM)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-IAM-01 | Users sign in with email and password through Supabase Auth; email must be verified; password reset and MFA recovery flows exist and are audited. | P0 | 2–3 |
| FR-IAM-02 | MFA (TOTP) is mandatory for Owner and Admin roles. Sensitive actions require a session at MFA level `aal2`. | P0 | 2–3 |
| FR-IAM-03 | Role-based access control with roles Owner, Admin, Operations, Finance, Support, Developer, Viewer, scoped per organization and per shop. Deny by default. | P0 | 2 |
| FR-IAM-04 | Authorization is decided on the server from the authenticated context. Client-supplied `user_id`, `organization_id` or `shop_id` are never trusted as authority. | P0 | 2 |
| FR-IAM-05 | Users can list and revoke their active sessions and devices. | P1 | 3 |
| FR-IAM-06 | Recent re-authentication (step-up) is required for high-risk actions and approvals (money, publishing, permissions). On Android and Windows this can use a biometric-protected device key whose signature the server verifies. | P1 | 3, 7, 16–17 |
| FR-IAM-07 | Users, AI employees, automations, the Decision Advisor and system components have separate identities and separate permission sets. An employee can never approve its own request. | P0 | 7–8 |

### 7.2 Shops and integrations (SHOP)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-SHOP-01 | The owner connects an Etsy shop through OAuth 2.0 with PKCE, using an Etsy **Seller App** registered to that shop (one app per shop account). Tokens are stored encrypted on the server only and refreshed automatically (access token 1 hour, refresh token 90 days, with an alert before refresh expiry). | P0 | 4 |
| FR-SHOP-02 | A shop card shows connection status, granted scopes, last successful sync, and verified capabilities (for example, whether shipment updates are permitted for this app). | P0 | 4 |
| FR-SHOP-03 | Connecting a shop does not grant every user or agent access to it. Access is granted per shop. | P0 | 4 |
| FR-SHOP-04 | Disconnecting a shop deletes its tokens from our storage (and revokes them at Etsy where Etsy supports it; the owner can also remove the app in Etsy's settings), stops sync, and starts the retention timer for that shop's data. | P0 | 4–5 |
| FR-SHOP-05 | Marketplace access goes through a `MarketplacePort` adapter from the first integration, so Shopify or another platform can be added later without changing domain code (the Shopify adapter itself is P2). | P1 | 4, 17 |

### 7.3 Orders and fulfillment (ORD)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-ORD-01 | Paid Etsy orders are ingested into `orders` and `order_items` by deterministic code, with no LLM involved. Etsy order webhooks (`order.paid`, `order.canceled`, `order.shipped`, `order.delivered`) trigger ingest when they are available to our app type; a reconciliation poller runs regardless, so a missed webhook never means a missed order. | P0 | 5 |
| FR-ORD-02 | Ingestion is idempotent: duplicate, replayed or out-of-order events never create duplicate orders or inflate sales counts. | P0 | 5 |
| FR-ORD-03 | Orders follow a state machine derived only from observable facts: `PAID → INGESTED → AWAITING_SHIPMENT → SHIPPED → DELIVERED`, plus `CANCELLED` and `REFUNDED`. `SHIPPED` means a shipment record exists on the marketplace; its tracking code is recorded when present. `SUPPLIER_ORDERED` is added once supplier-side evidence is available (Phase 13). Problems are incidents linked to the order, not order states. Each transition records its source of proof. | P0 | 5–6 |
| FR-ORD-04 | Orders list with search, sort, filters (shop, status, date, tracking state) and safe bulk actions. | P0 | 5 |
| FR-ORD-05 | Order detail shows items, SKUs, payment event, tracking carrier and code, timeline with who did what and when, incidents, and audit; supplier mapping and AutoVA data appear once available (Phase 13). | P0 | 5–6, 13 |
| FR-ORD-06 | Tracking numbers are detected from the marketplace: AutoVA writes them to Etsy, and we read them from the receipt's shipment records (and the `order.shipped` webhook). The system never writes a second shipment update if one already exists. | P0 | 6 |
| FR-ORD-07 | Writing a shipment to Etsy is a guarded, human-confirmed fallback only. Etsy restricts this endpoint for new apps in several regions (including the US and Israel), every successful call emails the buyer, and the endpoint has no idempotency. The capability is probed, never assumed; before any write the receipt is re-read and the write is skipped if that tracking code is already present. A 403 shows "capability blocked" with the allowed alternative, never a retry loop. | P1 | 6 |
| FR-ORD-08 | Units sold are counted from `order_items.quantity`, separately from order count, net of cancellations and refunds. | P0 | 5 |
| FR-ORD-09 | A reconciliation job detects drift between our records and the marketplace and opens an incident when they disagree. | P1 | 5 |

### 7.4 Incidents and debugging (INC)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-INC-01 | Fulfillment SLA rules per shop, supplier and shipping method open an incident when an order has no tracking after its window. A late order is not an incident until its window passes. | P0 | 6 |
| FR-INC-02 | Incidents follow a state machine: `DETECTED → TRIAGED → INVESTIGATING → REMEDIATION_PROPOSED → (AUTO_REMEDIATING │ APPROVAL_REQUIRED │ DEVELOPER_REQUIRED) → VERIFYING → RESOLVED`, or `ESCALATED`. An unhandled incident is never marked resolved. | P0 | 6 |
| FR-INC-03 | Exactly one open incident exists per root cause and resource (deduplication). | P0 | 6 |
| FR-INC-04 | Amir (Debugger & Reliability Engineer) may only run remediations from an allowlist that are reversible and idempotent. Anything touching money, permissions, production code or customers needs approval or a developer. Until Amir is active, remediation is allowlisted code or a person. | P0 | 6, 19 |
| FR-INC-05 | An incident is resolved only on verified evidence (for example, tracking now present in Etsy), never on an agent's statement. | P0 | 6 |
| FR-INC-06 | Incident center: severity, impacted shops, reproduction steps, masked traces, proposed fix, verification and rollback. | P1 | 6 |

### 7.5 Tasks and approvals (TASK, APR)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-TASK-01 | Every unit of work is a task record with queue, SLA, assignee (a person, an employee or an automation), idempotency key and trace ID. A finished conversation is never proof that a task completed. | P0 | 7 |
| FR-TASK-02 | Task states: `CREATED, QUEUED, CLAIMED, RUNNING, WAITING_FOR_EXTERNAL, WAITING_FOR_AGENT, WAITING_FOR_APPROVAL, RETRY_SCHEDULED, BLOCKED, COMPLETED, FAILED, ESCALATED, CANCELLED`. | P0 | 7 |
| FR-TASK-03 | Tasks survive restarts. Two workers can never run the same task at once (atomic claim with lease and heartbeat). Failures retry with backoff and land in a dead-letter queue. | P0 | 5, 7 |
| FR-TASK-04 | Task views: board by state, list with SLA, and a timeline or dependency view. | P1 | 7 |
| FR-APR-01 | An approval shows the action, its impact, a cost range, risk level, reason, rollback plan and expiry. | P0 | 7 |
| FR-APR-02 | Approve and reject are re-authorized on the server at decision time and store the approver identity and `policy_version`. The UI alone never grants approval. | P0 | 7 |
| FR-APR-03 | Expired approvals cannot be executed. Deciding twice is idempotent. | P0 | 7 |
| FR-APR-04 | Approval policy defines, per capability and risk level, whether an action is automatic, needs one approver, or needs re-authentication. | P0 | 7 |

### 7.6 AI workforce (AGT)

See §8 for the organization and [ADR-0012](../adr/0012-living-office-organization.md).

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-AGT-01 | Each employee has a permanent identity (ID, name, title, department) and a versioned profile with a 22-section Agent Specification Sheet: mission, goals, responsibilities, non-responsibilities, input and output schemas, triggers, tools, automations, skills, knowledge, memory, communication, model routing, Decision Advisor use, budgets, risk and approvals, evaluation, failure handling, versioning and metrics. Each run records the profile version it used. | P0 | 8 |
| FR-AGT-02 | Agents are not always-on processes. Runs start from queued tasks, events or schedules, and a run that waits (for a supplier quote, an approval, an external event) is checkpointed and resumed later instead of polling. | P0 | 8 |
| FR-AGT-03 | Agent tools are typed capabilities (for example `catalog.classify`, `commerce.create_draft`, `incidents.open`) with validation, timeout, retry limit, side-effect level and audit. Creating a draft and publishing live are different capabilities. | P0 | 8 |
| FR-AGT-04 | The backend policy engine decides whether a tool call is allowed. If a model asks for a tool it is not permitted to use, the call is denied and recorded. | P0 | 8 |
| FR-AGT-05 | Content from the web, supplier pages, emails and marketplace messages is untrusted data. It can never change an agent's permissions, instructions or tools. | P0 | 8 |
| FR-AGT-06 | Four memory layers are kept separate: working memory (checkpoints), professional memory per agent, organizational memory (policies, runbooks), and business state in Postgres. Every memory item has a source, date, shop, version, confidence and expiry. | P1 | 8–15 |
| FR-AGT-07 | Agents hand off work through events and resource references stored in the database, not by passing whole conversation state. | P0 | 8 |
| FR-AGT-08 | Agent conversations show business messages and action receipts, never hidden model reasoning. | P1 | 15 |
| FR-AGT-09 | An employee or automation is production-ready only with every specification section defined and a passed evaluation; a configuration file existing is never enough. The owner approves each autonomy level. | P0 | 8+ |
| FR-AGT-10 | The organization is framework-independent: employees, tasks, automations, tools and records are defined and inspected without importing the agent runtime; runtimes implement `invoke`, `resume`, `interrupt`, `cancel` and `inspect`. Runtime state is never the source of truth for business records. | P0 | 8 |

### 7.7 Model routing and cost control (MDL)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-MDL-01 | All model calls go through the Model Gateway. No employee or package calls a provider SDK directly. | P0 | 9 |
| FR-MDL-02 | One central router serves every employee, each with its own versioned routing policy (allowed providers, required capabilities, default and maximum tier, per-task budget, maximum data class, fallback tiers, advisor use). Deterministic tasks route to `NO_AI`. | P0 | 9 |
| FR-MDL-03 | A model catalog stores verified provider model IDs, API version, class (economy, balanced, advanced, critical, vision, image), context size, prices with an as-of date, health, and eval scores. Model names are never hard-coded in agent code. | P0 | 9 |
| FR-MDL-04 | A budget ledger reserves cost before a call and settles the actual cost after, per organization, shop, agent, task, day and month. Alerts fire at 70%, 90% and 100%. New work stops at the cap, with a reserved budget kept for safety-critical tasks. | P0 | 9 |
| FR-MDL-05 | Every routing decision is recorded in `routing_decisions` with employee, trace ID, policy version, cost and quality outcome. | P0 | 9 |
| FR-MDL-06 | Fallback chains, a circuit breaker per provider and a kill switch per provider. | P1 | 9 |
| FR-MDL-07 | Customer PII is not sent to a model unless the task needs it and the employee's policy allows that data class; PII is masked by default. | P0 | 9 |
| FR-MDL-08 | Before relying on a decision provider (such as Jev) or a cheaper model for a decision, it is benchmarked on at least 200 labeled examples against rules-only and an economy model (accuracy per group, false-approve rate, schema validity, p95 latency, cost per decision). | P1 | 9 |
| FR-MDL-09 | A shared Decision Advisor answers typed questions (choice or score) through a provider-independent port: deterministic rules first, then providers in order, with schema validation and a record of every attempt. It never authorizes; security, payment and permission policies stay with the policy engine. | P0 | 9 |

### 7.8 Catalog and Product Studio (CAT)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-CAT-01 | A product (internal) is separate from a listing (per shop). One product can have several listings with different titles and prices. | P0 | 12 |
| FR-CAT-02 | The new-product pipeline: research, sourcing and policy screening (Shon) → copy (Noam) and images (Nika) in parallel → catalog assembly and pricing (Daniel) → risk-based review (Noa) → draft listing → approval → publish (automation). | P0 | 12 |
| FR-CAT-03 | Classification returns `APPROVED │ REVIEW │ REJECTED` (plus `NOT_ALLOWED_ON_ETSY`) with reasons and sources, covering niche fit, marketplace policy, intellectual property and trademarks. Hard rules and the Decision Advisor run before any model; a model is never the only policy gate. | P0 | 12 |
| FR-CAT-04 | Copy never invents product attributes or claims. Every factual claim (size, material, contents, care) is traceable to supplier or owner data. | P0 | 11–12 |
| FR-CAT-05 | Images keep provenance (source, edits, model, prompt version) and must not misrepresent the product: true colour, true scale, no extra items that are not included. Marketplace photo and AI-disclosure rules are enforced as policy checks (Etsy generally expects photos of the actual item and limits mockups; its creativity standards require disclosing AI-created items). The policy references are re-verified at the start of Phase 12. | P0 | 12 |
| FR-CAT-06 | Pricing is computed in code with `Decimal`: purchase cost, shipping, marketplace fees, taxes where relevant, competitor reference and a minimum margin floor. | P0 | 12 |
| FR-CAT-07 | The Catalog Draft Creation & Publishing automation (not an employee) creates drafts first, verifies the API result, and publishes only after approval and policy checks. Listing IDs, variations and history are stored. | P0 | 12 |

### 7.9 Listing Health (QA)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-QA-01 | An audit compares every active listing's claims (dimensions, material, included items, colour, photos) with supplier facts and owner data, and scores accuracy risk (Daniel). | P0 | 11 |
| FR-QA-02 | Findings become suggested fixes (title, description, attributes, photo order, scale reference) that the owner approves before anything changes on Etsy (Noam drafts the copy). | P0 | 11 |
| FR-QA-03 | Accuracy findings are measured against an owner-labeled sample (precision and recall) before the audit runs unattended. | P1 | 11 |

### 7.10 Suppliers and procurement (SUP)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-SUP-01 | A supplier quote is requested once per product or variant, not per order. The task waits in `WAITING_FOR_QUOTES` for days without running a model (Supplier Quote Request & Follow-up automation; Tom evaluates). | P0 | 13 |
| FR-SUP-02 | Hard rules filter invalid quotes (wrong variant, out of stock, price or shipping outside policy). Ranking among valid quotes may use AI only after its accuracy is benchmarked; money math stays in code. | P0 | 13 |
| FR-SUP-03 | An approved quote becomes an active supplier mapping that AutoVA uses for future orders. Changes in price, stock or validity open a re-sourcing task. | P0 | 13 |
| FR-SUP-04 | AutoVA is reached only through an `AutoVaPort` adapter. Preferred order: official API or integration, then a manual-entry adapter, and only as a last resort an isolated browser worker within AutoVA's terms (no CAPTCHA or MFA bypass). | P0 | 0, 13 |

### 7.11 Finance (FIN)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-FIN-01 | Financial entries (revenue, marketplace fees, supplier cost, shipping, refunds, payouts, AI and infrastructure cost) are recorded from events by code (Finance Ledger Ingestion automation; Roni reviews). | P0 | 5, 13 |
| FR-FIN-02 | P&L per shop, product, order and platform. Every figure is marked `ESTIMATED` or `VERIFIED`, with the exchange-rate source and payout lag. | P1 | 13 |
| FR-FIN-03 | Unit costs: cost per listing, per resolved ticket, per inspected order, per repair, per shop, per employee and department. | P1 | 13, 18 |
| FR-FIN-04 | CSV export requires permission and writes an audit record. | P1 | 13 |

### 7.12 Customer service (SVC)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-SVC-01 | Ticket queue with conversation timeline, SLA and order context, limited to shops the user may access. | P1 | 14 |
| FR-SVC-02 | Agents draft replies; sending requires approval according to risk, and only through channels the marketplace allows. Etsy's API has no buyer-messaging endpoint and usually no buyer email, so Etsy conversations are handled as *assisted replies*: the agent drafts, a person sends it in Etsy, and the system records it as sent manually. Email replies go only to people who contacted `support@` or `info@` directly (Dana). | P1 | 14 |
| FR-SVC-03 | Customer data is minimized and masked by default; deletion and export follow the retention policy. | P0 | 14 |

### 7.13 Marketing (MKT)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-MKT-01 | Listing performance analytics, SEO improvements, sales and coupons through the marketplace's own tools. Etsy's API exposes only basic listing stats (views, favourites) and no traffic analytics, so performance metrics combine those with our own order data (Gal). | P2 | 21 |
| FR-MKT-02 | Buyer data from Etsy is never turned into a marketing list. Consent is tracked in a consent ledger for any channel that needs it. | P0 | 14+ |

### 7.14 Email (EML)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-EML-01 | Phase A: branded transactional email and internal alerts from `noreply@office.ht-co.app` through one provider adapter. | P0 | 6 |
| FR-EML-02 | Phase B: shared inbound for `support@` and `info@` creates CRM threads and tickets through signed, idempotent webhooks (Email Intake & Routing automation). | P1 | 14 |
| FR-EML-03 | Inbound mail is untrusted: spam and attachment policy, HTML sandboxing, no remote content by default, and no instructions executed from email content. | P0 | 14 |
| FR-EML-04 | Phase C and D: drafting with approval, then per-agent aliases (`<slug>@agents.ht-co.app`) with scoped send rights after abuse tests pass. Names are not authority; only an authorized service identity can send as an employee. | P2 | 15+ |

### 7.15 Notifications (NTF)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-NTF-01 | In-app notifications for incidents, approvals, budget thresholds, stuck tasks and completed repairs. | P0 | 6–7 |
| FR-NTF-02 | Push on Android (FCM), Windows notifications, and web push with consent; deep links open the exact item. | P1 | 15–17 |
| FR-NTF-03 | Preferences per severity and per shop. | P1 | 7 |

### 7.16 Office and conversations (OFC)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-OFC-01 | The office shows departments and employees with states `IDLE, RUNNING, WAITING, NEEDS_APPROVAL, BLOCKED, OFFLINE`, driven only by recorded activity, never inferred or fabricated. | P1 | 15 |
| FR-OFC-02 | When the agent worker is offline, the office opens read-only and never shows an employee as running. | P0 | 15 |
| FR-OFC-03 | An accessible list view carries the same information as the visual office; animation respects reduced motion. | P0 | 15 |
| FR-OFC-04 | An employee profile shows name, role, goals, activity, tools, skills, permissions, automations, sessions, memory summaries, model policy, evaluations and costs. | P1 | 15 |
| FR-OFC-05 | Office Communication: conversations with Lia, Maya, one employee or a department; internal employee-to-employee messages; task threads and handoff history; searchable; approval cards inline. | P1 | 15 |
| FR-OFC-06 | The Executive Office shows the manager overview, Noa's findings, Omer's proposals, approvals and business health. | P1 | 15 |
| FR-OFC-07 | The Automation Center shows workflow definitions, triggers, executions, failures, retries and metrics, and allows authorized manual runs; the AI Tools Center shows tools, versions, status, capabilities, access and tests. | P1 | 15 |
| FR-OFC-08 | The Decision & Model Observatory shows the Decision Advisor's status, routing policies, model usage, costs, routing history and evaluation results. | P1 | 15 |

### 7.17 Administration and developer console (ADM)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-ADM-01 | Manage users, roles, shop permissions and MFA enforcement. | P0 | 2–3 |
| FR-ADM-02 | Secret references, token rotation status and provider key metadata, never secret values. | P1 | 4 |
| FR-ADM-03 | Feature flags, per-employee model routing policies, budgets, kill switches per employee and automation, and notification policies. | P1 | 8–9 |
| FR-ADM-04 | Engineering Console: engineering requests, incidents, optimization proposals, pull requests, deployments, releases and infrastructure health; safe actions in staging, by role. | P1 | 15, 17, 19 |

### 7.18 Audit and observability (AUD)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-AUD-01 | Every money, permission, publishing and approval action writes an append-only audit record: actor, role, organization, shop, entity, correlation ID, time, outcome. The UI cannot edit audit records. | P0 | 2+ |
| FR-AUD-02 | Structured logs with `trace_id`, `shop_id` and no PII or secrets; error monitoring; worker heartbeats; queue age, backlog and retries; budget metrics. | P0 | 1+ |
| FR-AUD-03 | Health endpoints `/healthz` (process alive) and `/readyz` (dependencies ready). | P0 | 1 |
| FR-AUD-04 | Every meaningful operation (task step, automation execution, business event, tool invocation, message, decision, approval, incident, routing decision, engineering change, reconciliation, quality finding) writes an activity record with actor kind and ID, organization, shop, task, correlation and causation IDs, timestamps and outcome. Records hold business summaries, never private model reasoning, secrets or unnecessary PII. | P0 | 2, 7 |
| FR-AUD-05 | Activity is queryable by actor, department, shop, task, kind and time, answering questions such as "what did Shon do today", "which automations failed", "did every Etsy order reach the CRM", "why did an employee choose a model". | P1 | 7, 15 |

### 7.19 Automation Department (AUT)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-AUT-01 | Every automation is a versioned definition with an 18-section specification (owner, trigger, schemas, preconditions, steps, dependencies, errors, retries, idempotency, timeouts and SLA, permissions, audit, tests, recovery, approvals, metrics) ([ADR-0013](../adr/0013-automation-engine.md)). | P0 | 5 |
| FR-AUT-02 | One entry point serves every trigger kind (event, schedule, authorized agent tool, user action, workflow), each through the API with authorization. Employees trigger only automations granted to them, through the `automation.trigger` tool. | P0 | 5, 8 |
| FR-AUT-03 | Executions are unique per automation and idempotency key; replays never repeat a business effect; failures retry with backoff and end in a dead-letter state with an incident. | P0 | 5 |
| FR-AUT-04 | Automations call no model unless a named step needs reasoning, and that step goes through the router like any employee call. | P0 | 5+ |
| FR-AUT-05 | The 15 initial workflows are those in `registry/automations/`; each is activated in its phase after its tests pass. | P0 | 5–18 |

### 7.20 AI Tools Department (TOOL)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-TOOL-01 | Every tool is a versioned definition with owner, family, side-effect class, risk, data scope, audit requirement, readiness and contracts ([ADR-0014](../adr/0014-tool-registry.md)). | P0 | 8 |
| FR-TOOL-02 | Employees receive least-privilege grants; read-only and mutating tools are separate; external and financial tools need approval below autonomy L3 and critical tools always. | P0 | 8 |
| FR-TOOL-03 | Every tool invocation is recorded with employee, profile version, tool version, decision and outcome. | P0 | 8 |

### 7.21 Quality and operations supervision (QOS)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-QOS-01 | Deterministic monitors verify event completeness, state transitions, deadlines, worker heartbeats and leases, queue health and required audit records, without model calls. | P0 | 10 |
| FR-QOS-02 | Noa receives actionable exceptions, samples completed work by risk, reviews high-risk operations and checks results against business requirements and external evidence; findings carry evidence and a follow-up owner. | P0 | 10 |
| FR-QOS-03 | A nightly supervisor finds unresolved, stalled and overdue tasks and expired sessions or leases, reconciles business status with external evidence, resumes or reassigns safely restartable work, opens incidents, requests approval for risky repairs and produces the daily operations report. | P0 | 10 |
| FR-QOS-04 | Corrections are verified by follow-up; a finding closes only on evidence. | P0 | 10 |

### 7.22 Continuous optimization (OPT)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-OPT-01 | Cost, latency, failure, retry, tool-use and quality metrics are collected per employee, department, workflow and unit of work. | P1 | 13, 18 |
| FR-OPT-02 | Optimization proposals follow `DETECTED → PROPOSED → EVALUATING → APPROVED / REJECTED → DEVELOPMENT → TESTING → DEPLOYED → MEASURING → VERIFIED / ROLLED_BACK`, each with baseline, evidence, affected tasks, proposed change, whether it can become deterministic, effort, expected benefit, risk, rollback plan and acceptance criteria. | P1 | 18 |
| FR-OPT-03 | Omer proposes and measures; he never implements, approves his own proposals or changes policies directly. | P0 | 18 |

### 7.23 Engineering and self-improvement (ENG)

| ID | Requirement | Pri | Phase |
|---|---|---|---|
| FR-ENG-01 | Engineering requests come from the owner, Maya, Noa's findings, incidents, Omer's proposals, agent improvement proposals, monitoring and UX opportunities, and link to GitHub issues, branches, pull requests, tests and deployment records. | P1 | 19 |
| FR-ENG-02 | Alex owns software and architecture, Amir reliability and remediation, Iris agent systems, tools and office experience; no duplicate implementations. | P1 | 19 |
| FR-ENG-03 | Changes to code, databases, permissions, billing, integrations or production follow review, CI, staged release, feature flags and the owner's release gate. No engineering employee may weaken tests, remove logging, widen its own permissions or bypass branch protection. | P0 | 19 |

## 8. The living office

Version 1.0 of this PRD defined 18 logical agent roles. They are superseded by the organization below ([ADR-0012](../adr/0012-living-office-organization.md), which keeps the mapping from each old role). Four concepts never mix: **AI employees** (judgment, language, decisions), **automations** (deterministic workflows), **tools** (capabilities, no objectives) and **engineering** (building and improving the office).

Autonomy levels: **L0** read-only and suggestions, **L1** reversible actions inside the system, **L2** external actions after approval, **L3** external actions within a pre-approved policy. Each employee starts at the lowest level that lets it do its job and is promoted only after its evaluation gate passes and the owner approves.

### 8.1 Employees

| Department | Employee | Core responsibility | Start level | Phase |
|---|---|---|---|---|
| Executive Office | **Maya** — General Manager | Turns the owner's objectives into plans and tasks across departments; tracks progress and exceptions; escalates owner decisions; runs daily and weekly reviews | L1 | 15 |
| Executive Office | **Noa** — Quality & Operations Supervisor | Independently verifies work against evidence: exceptions from monitors, risk-based sampling, high-risk review, handoffs, corrective tasks, the nightly review | L1 | 10 |
| Executive Office | **Omer** — Continuous Optimization Officer | Measures cost, speed and quality; turns waste (often AI that could be code) into proposals with baselines; measures results | L0 | 18 |
| Executive Office | **Lia** — Owner Assistant | The owner's interface: conversations, reports, alerts, approval requests, task tracking | L1 | 15 |
| Product Research, Content & Catalog | **Shon** — Product Intelligence | Competitor research, sourcing, supplier and variation discovery, opportunity analysis, policy and IP screening → APPROVED / REVIEW / REJECTED | L0 | 12 |
| Product Research, Content & Catalog | **Noam** — SEO & Copywriting | Titles, descriptions, tags, FAQs from verified facts, per store | L1 | 11 |
| Product Research, Content & Catalog | **Nika** — Creative & Media | Authorized images and media with provenance and usage rights | L1 | 12 |
| Product Research, Content & Catalog | **Daniel** — Catalog & Pricing Architect | Listings, SKUs, variations, consistency checks, Decimal pricing; the publish-ready specification; the Listing Health audit | L1 | 11 |
| Procurement & Finance | **Tom** — Procurement & Supplier Quotes | Initial quotes and supplier mappings, exceptions, commercial recommendations | L2 | 13 |
| Procurement & Finance | **Roni** — Finance Controller | Revenue, costs, fees, refunds, profitability and AI cost; verifies reconciliation discrepancies; forecasts | L0 | 13 |
| Order Assurance | **Eitan** — Fulfillment Inspector | Investigates fulfillment exceptions and discrepancies between Etsy, AutoVA and the CRM; verifies recovery | L1 | 9 |
| Marketing | **Gal** — Marketing & Growth | Performance analysis, growth opportunities, compliant campaigns and experiments | L0 | 21 |
| Customer Service | **Dana** — Customer Service | Inquiries through authorized channels, order lookup, drafted or permitted replies, escalation | L2 | 14 |
| Engineering & Development | **Alex** — Lead Software & Platform Engineer | Features, automations, infrastructure, APIs; implements approved proposals; tests, migrations, docs, pull requests | L1 | 19 |
| Engineering & Development | **Amir** — Debugger & Reliability Engineer | Incidents, logs and traces, root causes, allowlisted reversible repairs, regression tests, verified recovery | L1 | 19 |
| Engineering & Development | **Iris** — Agent Systems & Experience Engineer | Agent definitions, skills, tools, prompts, memory and evaluations; the office experience; prototypes and pull requests, never unreviewed production changes | L1 | 19 |

Each employee's draft Agent Specification Sheet is in [`registry/employees/`](../../registry/employees). Sixteen employees are sixteen *profiles*, not services, repositories, containers or mailboxes; they share the `agent-worker`.

### 8.2 The Decision Advisor

**Jev** (TypeSafe's decision model) is a shared service behind the `DecisionAdvisor` port, with an identity and activity history in the office but no department and no headcount. Rules decide first; the advisor answers typed triage and ranking questions only when rules are inconclusive, and never authorizes anything. It is benchmarked before production use (FR-MDL-08, FR-MDL-09).

### 8.3 Automations

The Automation Department's 15 initial workflows ([`registry/automations/`](../../registry/automations)): Etsy Order Intake; Order Deduplication & Verification; Product Sales Counters; Catalog Draft Creation & Publishing; Supplier Quote Request & Follow-up; Tracking Synchronization; Finance Ledger Ingestion; Inventory & Supplier Price Monitoring; Order & Financial Reconciliation; Incident Detection & Escalation; Email Intake & Routing; Scheduled Reporting & Notifications; Agent Task Scheduling & Recovery; Quality Review Scheduling; Optimization Review Scheduling. Each has a business owner among the employees and is activated in its phase.

### 8.4 Tools

Forty initial tools in twelve families (Office & CRM, Commerce & Catalog, Supplier Research, Research & Retrieval, Finance & Pricing, Creative & Media, Communication & Email, GitHub & Development, Infrastructure & Observability, Decision Advisor, Model Gateway, Automation Execution) are in [`registry/tools/`](../../registry/tools).

## 9. Key flows

### 9.1 Order to delivery (no AI in the normal path)

1. Etsy sends an `order.paid` webhook (signed; the payload only points to the receipt). A reconciliation poller also asks Etsy for recently modified receipts every few minutes, so nothing depends on a single webhook.
2. The ingest worker verifies the event, fetches the receipt, and upserts the order and items in one transaction keyed by `shop_id + external_id`, applying an update only if Etsy's `updated_timestamp` is newer than what we hold.
3. AutoVA, already connected to the shop, places the supplier order using the approved supplier mapping.
4. When AutoVA adds tracking, it appears in the receipt's shipment records (and as an `order.shipped` webhook). The tracking worker records it and moves the order to `SHIPPED` with the tracking code.
5. If no tracking appears within the SLA window, the Incident Detection & Escalation automation opens one incident; Eitan investigates the exception, and Amir applies an allowlisted fix or escalates.
6. The incident resolves only when the evidence (tracking present) is verified.

### 9.2 Approval

1. An employee or automation creates an approval with action, impact, risk, cost range, rollback and expiry.
2. The owner is notified (in app; push on Android later).
3. On decision, the server re-checks permissions, MFA level, expiry and policy version, records the decision, and resumes the waiting task.

### 9.3 Listing Health (first content workflow)

1. A scheduled audit takes active listings with their supplier facts.
2. Daniel scores each listing for accuracy risk; Noam drafts concrete fixes; Noa samples the results by risk.
3. Fixes become approvals; approved fixes are applied through the Etsy API and verified.
4. Accuracy and cost per audited listing are recorded.

### 9.4 New product (Product Studio)

Research, sourcing and policy screening (Shon) → copy (Noam) and images (Nika) in parallel → catalog assembly and pricing (Daniel) → risk-based review (Noa) → draft listing → approval → publish (automation) → supplier quote onboarding (Tom) → performance tracking.

### 9.5 Supplier quote onboarding (once per product or variant)

Request in AutoVA → task waits `WAITING_FOR_QUOTES` (no model calls while waiting) → quotes arrive → hard rules filter → optional ranking → approval by risk → mapping activated → AutoVA uses it for future orders.

## 10. Non-functional requirements

| ID | Area | Requirement |
|---|---|---|
| NFR-SEC-01 | Security baseline | OWASP ASVS 5.0 Level 2 for the API and web; OWASP Top 10 for Agentic Applications (2026) controls for the agent layer; MASVS-aligned controls for native apps. |
| NFR-SEC-02 | Secrets | Secrets only in the hosting secret store and Supabase; never in code, clients, chat, logs or agent memory. Etsy and provider tokens rotate and can be revoked. |
| NFR-SEC-03 | Tenancy | Row-level security on every exposed table plus server-side authorization on every endpoint and tool. Cross-shop access returns 404 or 403, verified by automated tests. |
| NFR-SEC-04 | Webhooks | Signature verification, replay protection with clock tolerance, idempotency, rate limits and audit. |
| NFR-PRV-01 | Privacy | PII minimization, masking in reports and logs by default, retention policy per data class, data subject export and deletion. Compliance targets: Etsy API Terms, Israeli Privacy Protection Law (Amendment 13), GDPR for EU buyers where applicable. |
| NFR-REL-01 | Availability | API target 99.9% in production (target, not a contractual SLA). |
| NFR-REL-02 | Durability | Webhook or poll results enqueued within 10 s at p95; zero logical duplicate orders; work resumes after a worker is killed mid-task. |
| NFR-REL-03 | Recovery | Documented RTO and RPO; daily backups at minimum; restore drill before production data is relied on, then quarterly. |
| NFR-PERF-01 | API latency | p95 under 300 ms for read endpoints at pilot scale (measured, then revised). |
| NFR-PERF-02 | Clients | Web: Core Web Vitals "good" on the dashboard. Android: cold start under 1.5 s on a mid-range device. Windows: 60 fps in active office animations. |
| NFR-A11Y-01 | Accessibility | WCAG 2.2 AA on the web; TalkBack and Narrator usable; keyboard-only operation on web and Windows; reduced motion respected. |
| NFR-I18N-01 | Languages | Hebrew (RTL) and English (LTR) from day one; tracking numbers, SKUs and IDs stay LTR inside RTL text; locale-aware dates, numbers and currencies. |
| NFR-OBS-01 | Observability | Structured logs, traces with correlation IDs across API, queue, workers and agents; dashboards for queue age, error rate and budget. |
| NFR-COST-01 | Cost | Pilot infrastructure stays within the envelope in [COST_MODEL.md](../cost/COST_MODEL.md); AI spend is capped by the budget ledger; costs are reviewed at every phase gate. |
| NFR-MNT-01 | Maintainability | Clean/hexagonal boundaries enforced in CI; typed code (Python type-checked, TypeScript strict, Kotlin, C# nullable); test-first development; docs updated in the same pull request. |

## 11. Platforms and distribution

| Client | Stack | Distribution |
|---|---|---|
| Web | TypeScript, React, design tokens as CSS variables | DigitalOcean App Platform static site at `dash.ht-co.app` |
| Windows | C#/.NET, WinUI 3, Windows App SDK | Signed MSIX through GitHub Releases (later WinGet) |
| Android | Kotlin, Jetpack Compose | Google Play internal testing, then production |
| API and workers | Python, FastAPI, LangGraph (behind a port) | DigitalOcean App Platform: `api` web service and one `agent-worker` (employees and automations); an `automation-worker` splits out only when isolation or measured load justifies it; later an isolated `browser-runner`. No container per employee. |
| Data | Supabase Pro (Postgres, Auth, Storage, Queues) | Managed |

## 12. Success metrics

### 12.1 Readiness for limited autonomy (from the original brief)

The system is ready for limited autonomous operation when all of these hold:

1. An Etsy order is stored exactly once, even when the event arrives twice.
2. Order status and tracking agree between Etsy, AutoVA and the CRM.
3. An incident opens, Amir diagnoses it, and it is fixed safely or escalated to a developer.
4. Noa verifies every fix from evidence, not from an employee's statement.
5. Employees hand off work in a documented way, and a task survives a restart.
6. The dashboard shows real communication between employees and their real state.
7. Every commercial action is tied to the correct shop and respects permissions.
8. AI spend is attributable per product, order, agent and shop.
9. An agent can take a product from research draft to a valid, policy-compliant listing.
10. A new shop can be added through an adapter without cloning the workforce.
11. Every active employee and automation passed its evaluation and readiness gate; scenarios A–J of the testing strategy pass.
12. The office has improved at least one workflow with savings measured after deployment and no quality loss.

### 12.2 Business KPIs (baselined in Phase 5, tracked monthly)

- Late or missing tracking rate, and share detected before the buyer asks.
- "Not as described" reviews and refund rate (target: clear decline after Listing Health).
- Average review rating per shop.
- Time from product idea to published listing.
- Cost per listing, per inspected order, per resolved ticket.
- Owner hours per week spent on operations (self-reported).

## 13. Constraints and assumptions

- Hosting is DigitalOcean App Platform; data, auth and queues are Supabase Pro. No self-managed VPS.
- The agent runtime is LangGraph behind an adapter port; the decision is confirmed by the Phase 8 proof-of-concept gates. The organization (employees, automations, tools, records) never depends on it (FR-AGT-10).
- Model names, IDs and prices are never fixed in documents or code; they live in the verified model catalog.
- Etsy API capabilities depend on the app's access level and must be verified with the owner's real account.
- AutoVA has no public API documented as of 2026-10-14; integration depends on what AutoVA can provide.
- Open assumptions and their owners are tracked in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## 14. Out of scope for v1

Self-serve multi-tenant SaaS and billing; marketplaces other than Etsy (Shopify adapter is P2); a native iOS app; own mail servers; automated ad buying; an always-on browser session for agents; one container or repository per employee.

## 15. Top risks

| Risk | Impact | Mitigation |
|---|---|---|
| Marketplace policy: Etsy does not allow reselling or dropshipping of ready-made items (except craft and party supplies and vintage); production partners are allowed only for items the seller designs, and must be disclosed | Shop suspension | Owner decision on the catalog model before Product Studio (Phase 12); Shon's policy screening with hard rules (`NOT_ALLOWED_ON_ETSY`); policy references re-verified each phase; non-compliant products routed to other channels instead of Etsy |
| Etsy API access (Seller App vs Personal vs Commercial; shipment write restricted by region; no published default rate limits) | Features blocked | Seller App for the owner's shop; capability probes; read-first design; "capability blocked" UX; rate-limit budget per shop |
| No Etsy messaging API and no buyer email | Customer service cannot be automated end to end on Etsy | Assisted replies (agent drafts, human sends in Etsy); email automation only for direct `support@` and `info@` contacts |
| AutoVA has no API | Supplier and tracking integration gaps | Read tracking from Etsy; manual-entry adapter; ask AutoVA for an integration |
| AI cost overrun | Budget | Ledger with hard caps, NO_AI routing, caching, per-unit costing |
| Prompt injection through web pages or email | Unsafe actions | Untrusted-data handling, deny-by-default tools, approvals, injection test fixtures |
| Three native clients for a small team | Slow delivery | Web first, Android for approvals, Windows last; shared tokens and generated clients |
| A self-improving office changes itself unsafely | Outages, silent weakening of controls | Engineering requests through pull requests, CI, review and the owner's release gate; no employee may weaken tests, remove logging or widen its own permissions; Noa and Omer measure every change |
| An unbenchmarked decision provider in the critical path | Wrong triage at scale | Rules first; the advisor never authorizes; 200-example benchmark and fallback chain before use |

## 16. Release plan

The build is split into phases that each add a working, tested increment. See [PLAN.md](../plan/PLAN.md) for phases, milestones and gates, and [MVP.md](MVP.md) for what the MVP includes.

## Appendix A — Cross-platform acceptance scenarios

1. The owner gets a mobile notification about an incident and approves or rejects a sensitive fix after re-authenticating.
2. A Support user cannot read an order from a shop they are not allowed to see, on any of the three clients.
3. A tracking number updated in Etsy appears in all three clients with source and timestamp, without duplication.
4. The office opens read-only when the worker is offline and never shows a fake "running" agent.
5. A keyboard-only Windows user can find an order and open its incident detail.
6. Android TalkBack reads an approval card with its reason, impact and risk level.
7. In Hebrew (RTL) on the web, an English tracking number and digits keep their order.
8. Logout or session revocation clears sensitive cache, push registration and realtime subscriptions.
