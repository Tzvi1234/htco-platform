# Phase 15 — Mail, customer service and the owner assistant

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 8](phase-08-models-tools.md) (router, budgets, MCP tool gateway); [Phase 10](phase-10-fulfillment-incidents.md) (transactional email adapter, `noreply@`, incidents); live order context from [Phase 9](phase-09-etsy-connection.md). May run in parallel with Phases 11–14 |
| **Requirements** | FR-AGT-05, FR-AGT-06, FR-AGT-08, FR-AGT-09, FR-AUT-05, FR-EML-02, FR-EML-03, FR-EML-04, FR-MKT-02, FR-NTF-01, FR-OFC-05 (owner conversation with Lia), FR-SVC-01, FR-SVC-02, FR-SVC-03 |
| **Mandatory tests owned** | Test 14 on real mail (an email with prompt injection calls no protected tool); fixtures for test 14 passed in [Phase 6](phase-06-workforce-claude-runtime.md) |
| **Status** | Planned |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column and the acceptance tables below are the minimum.

## Progress

- [ ] 15.1 Inbound pipeline: webhook, inbox, fetch, attachments, quarantine
- [ ] 15.2 Email Intake & Routing automation: threads, tickets, customer linking
- [ ] 15.3 Quarantined extractor to a validated ticket schema
- [ ] 15.4 Dana's drafts and approval-to-send
- [ ] 15.5 Etsy assisted replies
- [ ] 15.6 Mail and support screens
- [ ] 15.7 Dana activated
- [ ] 15.8 Agent mail aliases and the Agent Mail Alias Routing automation
- [ ] 15.9 Lia activated: approvals, notifications, reports and the owner conversation
- [ ] 15.10 Test 14 on real mail

## Goal

Mail becomes part of the office, safely. `support@office.ht-co.app` and `info@office.ht-co.app` become a shared inbox inside HTCO, next to the `noreply@office.ht-co.app` sender that exists since Phase 10, all through one managed provider for inbound and outbound that keeps the inbound webhook and threading. The **Email Intake & Routing** automation verifies, quarantines, threads and links inbound mail to orders and customers, and routes it by rules (the Decision Advisor only when rules are inconclusive and its benchmark passed). **Dana** (Customer Service) classifies requests, reads order context within her limits, and drafts replies that people approve. Etsy buyer conversations get assisted replies (drafted in HTCO, sent by a person in Etsy), because Etsy offers no buyer-messaging API.

Two more things join the office:

- **Agent mail aliases.** `<employee-slug>@agents.ht-co.app` (for example `shon@agents.ht-co.app`) are virtual aliases on the same provider, not 16 mailboxes or servers. The **Agent Mail Alias Routing** automation turns mail to an alias into an untrusted message in that employee's inbox. **The sender address never grants access to data**: an email identity maps to an employee's service identity and its capabilities, nothing more. Sending as an alias needs an authorized service identity, approval by risk, and passed abuse tests (FR-EML-04).
- **Lia** (Owner Assistant) becomes the owner's single interface to the office: she presents approvals with impact and risk, sends notifications, delivers reports and explains them, and holds one unified conversation with the owner. **She gets no admin rights from her name or her role**: every decision is the owner's, made through the same server checks as any other approval.

## Why now

The router, budgets and tool gateway (Phase 8) and the transactional email and incident paths (Phase 10) exist, and real orders arrive (Phase 9), so customer mail can be linked to real orders and drafts can be governed like any other model call. Lia is activated here because by now enough employees, approvals and reports exist that the owner needs one place to receive and decide them; the complete office screens around her come in [Phase 16](phase-16-web-office.md).

## Scope

**In:**

- Mail DNS on mail-only hostnames: MX, SPF, DKIM and DMARC for `office.ht-co.app` (inbound for `support@`, `info@`, `security@`) and for `agents.ht-co.app` (aliases). These two hostnames are reserved for mail; the application office is served at `app.ht-co.app/office`, so no CNAME on a mail hostname is ever needed.
- The inbound webhook (signed, idempotent), content fetch, attachment copy within the provider's download window (verify the window at kickoff), attachment policy and scanning, HTML sandboxing with remote content off.
- Threads and tickets; a quarantined extractor (no tools) that turns an email into a validated schema.
- Dana drafting replies (sending needs approval); assisted replies for Etsy conversations with a "sent manually" record.
- Consent ledger (`customer_consents`); retention for messages (data class DC2).
- Agent mail aliases: the alias table, inbound routing to the employee's inbox, outbound sending as an alias behind approval and abuse tests; the new automation **Agent Mail Alias Routing**.
- Lia's activation: approvals presentation, notifications, reports, the owner conversation; Hermes as a channel if the owner decides so (D-10).
- Web screens: Mail Inbox / Outbox (clients specification screen 38), Agent Mail Alias Manager (39), Customer Service (34), and the owner conversation with Lia (screen 18, Employee Chat, for Lia only).

**Out:**

- Marketing email and any mailing list ([Phase 21](phase-21-growth-scale.md)). **No mailing list is ever built from Etsy buyer emails without permission and recorded consent** (FR-MKT-02).
- Conversations with every employee, employee-to-employee messenger screens, department conversations and web push ([Phase 16](phase-16-web-office.md)).
- Maya's full scope and the daily and weekly office reviews ([Phase 16](phase-16-web-office.md)).
- A mail server or mailbox per employee: never.

## Owner prerequisites

- DNS for `office.ht-co.app` and `agents.ht-co.app` (MX, SPF, DKIM, DMARC) with the DNS provider chosen in D-11; inbound enabled on the mail provider. Confirm that the provider supports inbound on a second subdomain with per-alias routing (verify at kickoff; do not assume).
- Reply policy: what Dana may promise (never refunds or compensation without approval), tone, languages.
- Labelled support examples for Dana's evaluation set and the routing benchmark.
- The list of employees that get an alias first (proposed: Shon only, as in the clients specification), and what each alias may receive and, later, send.
- D-10: Hermes as a channel for Lia, yes or no.
- Approval of Dana's autonomy (proposed L2: drafts, sending after approval), Lia's autonomy (proposed L1), Email Intake & Routing and Agent Mail Alias Routing.

## Instructions for the agent

1. Load the skills `software-engineering`, `tdd`, `testing`, `feature-verification` and `feature-docs`; for slice 15.6 and the Lia conversation screen in `htco-clients` also load `design-system`.
2. Read, in order: this file; [PLAN.md](../PLAN.md) §5–§6; [ADR-0016](../../adr/0016-claude-native-agent-runtime.md) (locked-down sessions, tool bridge), [ADR-0017](../../adr/0017-auth-and-billing-policy.md); [ENGINEERING_RULES §8](../../engineering/ENGINEERING_RULES.md#8-ai-employees-automations-and-tools-er-ai) (Rule of Two, ER-AI-11; masking, ER-AI-10); the [threat model](../../architecture/THREAT_MODEL.md); `DATA_MODEL.md` (`email_*`, `customers`, `customer_consents`, `support_tickets`), `EVENT_CATALOG.md`, `PERMISSION_MATRIX.md`; the registry files [`dana.toml`](../../../registry/employees/dana.toml), [`lia.toml`](../../../registry/employees/lia.toml), [`email-intake-routing.toml`](../../../registry/automations/email-intake-routing.toml), [`scheduled-reporting.toml`](../../../registry/automations/scheduled-reporting.toml), [`communication_email.toml`](../../../registry/tools/communication_email.toml).
3. Work the slices in order: 15.1 → 15.2 → 15.3 (the untrusted-input path first), 15.4 → 15.5 → 15.6 → 15.7 (Dana), 15.8 (aliases, after the inbound path is proven), 15.9 (Lia; may start after 15.1 since it does not depend on mail), 15.10 (last, on real mail).
4. For each slice: acceptance criteria → failing tests → code → verify for real → docs → one pull request.
5. Create `registry/automations/agent-mail-alias-routing.toml` with all 18 sections before writing its code (business owner: Lia).
6. Never in this phase: send any email without approval where the policy requires it; let mail content, a sender address or an alias name change a permission, a grant or an instruction; give Lia admin rights or the power to decide an approval; put buyer PII in a prompt that does not need it; build a mailing list from buyer emails; create a mailbox per employee; serve the app on a mail hostname.
7. Stop and ask the owner when: the provider cannot route a subdomain alias as planned; an abuse test fails for an alias; a drafted reply could commit money, a refund or compensation; the injection corpus finds a path to a protected tool; the owner's chat would need a capability Lia does not have.

## Employees and automations activated

| Kind | Name | Registry | Slices |
|---|---|---|---|
| Employee | Dana, Customer Service | [`dana.toml`](../../../registry/employees/dana.toml) | 15.4, 15.7 |
| Employee | Lia, Owner Assistant | [`lia.toml`](../../../registry/employees/lia.toml) | 15.9 |
| Automation | Email Intake & Routing | [`email-intake-routing.toml`](../../../registry/automations/email-intake-routing.toml) | 15.1, 15.2 |
| Automation | Agent Mail Alias Routing (**new**) | `agent-mail-alias-routing.toml` (created in 15.8) | 15.8 |

Employees follow the employee activation rule of [Phase 7](phase-07-pilot-office.md); automations the automation activation rule of [Phase 4](phase-04-durable-core-order-intake.md). Lia also becomes the active business owner of Scheduled Reporting (running since [Phase 5](phase-05-tasks-messages-approvals.md)).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 15.1 | Inbound pipeline: webhook, inbox, fetch, attachments, quarantine | Forged, replayed and oversized mail rejected; attachments scanned and stored safely; remote content off |
| 15.2 | Email Intake & Routing: threads, tickets, customer linking by order reference, rule-based routing | A reply lands in the right thread; no cross-shop linking; replaying a webhook creates no second ticket |
| 15.3 | Quarantined extractor → validated ticket schema | Injection corpus in emails causes no tool call and no schema escape |
| 15.4 | Dana's drafts and approval-to-send | Nothing is sent without approval; sends are idempotent and audited; Dana never promises refunds or compensation |
| 15.5 | Etsy assisted replies | Draft copy-to-clipboard and "mark as sent" with audit |
| 15.6 | Mail and support screens: Mail Inbox / Outbox (38), Customer Service (34), templates, sending approvals, delivery failures, spam and injection flags, provider health | PII masked by default; role- and shop-limited access; loading, empty, error, permission and offline states |
| 15.7 | Dana activated | Sheet defined, evaluation passed, autonomy approved, routing measured (including the data-class limit of her policy) |
| 15.8 | Agent mail aliases and Agent Mail Alias Routing | See below |
| 15.9 | Lia activated | See below |
| 15.10 | Test 14 on real mail | See below |

### 15.8 — Agent mail aliases and the Agent Mail Alias Routing automation

An alias table (`agent_mail_aliases`, or an extension of `email_identities`; decided in DATA_MODEL at kickoff) maps `<employee-slug>@agents.ht-co.app` to one employee, with status, inbound restrictions, outbound rights and delegation. The Agent Mail Alias Routing automation (webhook trigger) verifies the provider signature, deduplicates, quarantines the content through the extractor of 15.3, and delivers it to the employee as an `employee_messages` row marked untrusted, or as a task when the routing rule says so. Outbound mail as an alias uses `email.send` under the employee's service identity, through the outbox, with approval by risk. The Agent Mail Alias Manager screen (39) shows aliases, mappings, restrictions, delegation and recent traffic.

| ID | Acceptance criterion |
|---|---|
| AC-15.8.1 | Mail to an active alias creates exactly one untrusted message for the mapped employee; a replayed webhook creates nothing new. |
| AC-15.8.2 | Mail to an unknown or disabled alias creates no message or task for any employee and is recorded as rejected. |
| AC-15.8.3 | The sender address and the alias name never grant access: mail "from the owner" or "from an admin" asking for data or an action leads to no protected tool call and no data release (the same bridge checks as any request). |
| AC-15.8.4 | Sending as an alias is refused unless the alias has outbound rights, the caller is that employee's service identity, the approval policy is met and the alias passed its abuse tests (rate limits, spoofing, reply loops, attachment abuse). |
| AC-15.8.5 | The automation's specification has all 18 sections defined; replay, failure and recovery tests pass. |
| AC-15.8.6 | DNS checks: `office.ht-co.app` and `agents.ht-co.app` carry only mail records (MX, SPF, DKIM, DMARC), and the app answers at `app.ht-co.app/office`. |

### 15.9 — Lia activated: approvals, notifications, reports and the owner conversation

Lia works from the owner's messages, approvals waiting for the owner, notifications and scheduled reports. She presents each approval with action, impact, cost range, risk, reason, rollback and expiry (FR-APR-01) and links to its records; she explains reports from their sources; she opens and tracks tasks for the owner and hands work to Maya. The owner talks to her in one conversation (an `employee_messages` thread on the Employee Chat screen for Lia; the same thread is reachable from Hermes if D-10 says yes). Her grants per the master plan (owner requests, report reading, approval reading, notification sending) are mapped to registry tools in the design review; she has no tool that decides an approval, changes a grant, role, budget or policy, or reads secrets.

| ID | Acceptance criterion |
|---|---|
| AC-15.9.1 | Lia's sheet has no `draft` section; her evaluation set (reports and approval presentations checked against records, refusals of out-of-scope requests) passes; the owner approved her autonomy; her routing is measured. |
| AC-15.9.2 | An approval presented by Lia and decided by the owner in the conversation is re-authorized on the server with the owner's identity and `policy_version`; Lia's identity never appears as the approver. |
| AC-15.9.3 | A request in the conversation, in an email or in a report asking Lia to grant access, change a role or approve something is refused and recorded; her name and role confer no admin rights (policy test over every registered tool). |
| AC-15.9.4 | Every figure Lia gives links to the records it came from; a report built while a source was missing says so. |
| AC-15.9.5 | Notifications Lia sends contain no buyer PII and deep-link to the exact item, which opens only after sign-in. |
| AC-15.9.6 | If Hermes is enabled (D-10), it can notify and deep-link but cannot decide; decisions always go through the HTCO server. |

### 15.10 — Test 14 on real mail

| ID | Acceptance criterion |
|---|---|
| AC-15.10.1 | **Master plan test 14 on real mail**: real emails sent to `support@` and to an agent alias, carrying the injection corpus (in body, HTML, subject, attachments and quoted replies), cause no protected tool call, no permission, memory or instruction change, and no outbound message; each is flagged as suspected injection in the inbox. |
| AC-15.10.2 | The Rule of Two holds for every mail path: the extractor reads untrusted mail with no tools; Dana and the alias recipient see only the validated schema, never raw HTML. |

## Test plan

Webhook security tests (signature, replay, size); the injection corpus on fixtures and on real mail; automation replay, failure and recovery tests for both automations; alias abuse tests (spoofed senders, unknown aliases, reply loops, rate limits); Dana's evaluation set (classification, reply quality, refusal of unsafe promises); Lia's evaluation set (reports and approvals against records, refusals); the routing benchmark if the advisor is used; real inbound mail end to end; screen tests with recorded fixtures.

## Security checklist

- [ ] FR-EML-03 controls; LINDDUN review for buyer messages; retention job for DC2 data.
- [ ] Rule of Two: the extractor reads untrusted mail with no tools; Dana never sees raw HTML.
- [ ] Instructions inside customer messages and alias mail are data; no grant changes from content, sender address or alias name.
- [ ] Lia cannot gain tools or rights from conversation content, her name or her role; approvals from any surface pass the same server checks.
- [ ] Mail hostnames carry only mail DNS records; SPF, DKIM and DMARC enforced.
- [ ] Consent ledger in place; no marketing list from buyer emails.
- [ ] The [threat model](../../architecture/THREAT_MODEL.md) updated for mail and aliases.

## Documentation

`docs/features/email-inbound/`, `docs/features/support/`, `docs/features/agent-mail-aliases/`, `docs/features/employees/dana.md`, `docs/features/employees/lia.md`; both automation specifications completed in `registry/`; DNS runbook for the two mail hostnames; DATA_MODEL, EVENT_CATALOG and PERMISSION_MATRIX updated.

## Demo

Send a real customer email that references an order; show the thread, the ticket linked to the order, Dana's draft, the owner's approval and the delivery proof. Send an email with an injection payload to `support@` and to `shon@agents.ht-co.app`; show the flags and that no tool ran. Ask Lia in the conversation what needs the owner today; approve one item from the conversation and show the server-side approval record with the owner as approver.

## Exit gate

- [ ] Real inbound mail handled end to end; test 14 passes on real mail; the owner approves the first sent reply.
- [ ] Dana, Lia, Email Intake & Routing and Agent Mail Alias Routing are `ready` and approved.
- [ ] At least one alias receives mail in production; outbound alias sending is enabled only for aliases that passed the abuse tests.

## Rollback

Dana's, Lia's and both automations' kill switches; aliases disabled one by one; inbound mail keeps arriving at the provider and can be re-ingested idempotently.

## Risks

| Risk | Mitigation |
|---|---|
| Spam and abuse through the inbound addresses and aliases | Provider spam filtering, attachment policy, rate limits per sender, aliases receive-only until their abuse tests pass |
| A drafted reply makes an unsafe promise | Approval before sending; evaluation cases for refusals; Noa samples sent replies (Quality Review Scheduling) |
| An alias or Lia's role is mistaken for authority | Identity maps only to a service identity and its grants; policy tests over every tool |
| Provider cannot route subdomain aliases as planned | Verified at kickoff before any DNS change; fall back to plus-addressing or a single routed mailbox, decided with the owner |

## Cost impact

The mail provider's plan for inbound and sending volume (verify current pricing at kickoff; inbound may count toward the sending quota). Dana's cost per resolved request and Lia's model usage, measured and recorded with `billing_mode = API_PRODUCTION`.
