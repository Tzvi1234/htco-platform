# Phase 14 — Email and customer service

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 10](phase-10-supervision.md) (can run in parallel with Phases 11–13) |
| **Requirements** | FR-AGT-06, FR-AGT-09, FR-AUT-05, FR-EML-02, FR-EML-03, FR-MKT-02, FR-SVC-01, FR-SVC-02, FR-SVC-03 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 14.1 Inbound pipeline: webhook, inbox, fetch, attachments, quarantine
- [ ] 14.2 Email Intake & Routing automation: threads, tickets, customer linking
- [ ] 14.3 Quarantined extractor to a validated ticket schema
- [ ] 14.4 Dana's drafts and approval-to-send
- [ ] 14.5 Etsy assisted replies
- [ ] 14.6 Support UI: inbox, ticket view, SLA
- [ ] 14.7 Dana activated

## Goal

`support@` and `info@office.ht-co.app` become a shared inbox inside HTCO: the **Email Intake & Routing** automation verifies, quarantines, threads and links inbound mail to orders and customers, and routes it by rules (the Decision Advisor only when rules are inconclusive and its benchmark passed); **Dana** (Customer Service) classifies requests, retrieves order context, and drafts replies that people approve. Etsy buyer conversations get assisted replies (drafted in HTCO, sent by a person in Etsy), because Etsy offers no messaging API.

## Scope

**Etsy buyer messages (owner, 2026-10-15).** Etsy has no messages API ([capabilities](../../architecture/etsy-api-capabilities.md)).
- **Default path:**
  - Dana reads Etsy's message notifications from the owner's mailbox.
  - She gathers the context: the order, its shipment and the conversation's history.
  - She drafts a reply. A person approves it with one click.
  - "Open in Etsy" opens the conversation with the reply copied, ready to paste and send.
- **Opt-in path (the owner's decision, one pilot shop first):** Dana sends only approved replies herself, from an isolated browser session.
  - Human pace and no disguise of any kind: no proxies, no fingerprint changes, no CAPTCHA solving.
  - Off by default, per shop.
  - This is automated use of etsy.com, which Etsy's terms restrict. The owner accepts that risk per shop. It is recorded as a decision with its own kill switch, and buyer-facing sends always need approval.


**In:** MX for `office.ht-co.app` to Resend inbound (addresses `support@`, `info@` and `security@`); the `email.received` webhook (signed, idempotent), content fetch, attachment copy within the provider's one-hour URL window, attachment policy and scanning, HTML sandboxing with remote content off; threads and tickets; a quarantined extractor (no tools) that turns an email into a validated schema; Dana drafting replies (L2: sending needs approval); the assisted-reply workflow for Etsy conversations with a "sent manually" record; consent ledger; retention for messages (data class DC2).

**Out:** per-employee addresses (FR-EML-04, [Phase 15](phase-15-office-executive.md) onwards); marketing email ([Phase 21](phase-21-growth-scale.md)).

## Owner prerequisites

- MX records for `office.ht-co.app`; Resend inbound enabled.
- Reply policy: what Dana may promise (never refunds or compensation without approval), tone, languages.
- Labelled support examples for Dana's evaluation set and the routing benchmark.
- Approval of Dana's autonomy level (proposed L2: drafts, sending after approval) and of the automation.

## Employees and automations activated

| Kind | Name | Registry |
|---|---|---|
| Employee | Dana, Customer Service | [`dana.toml`](../../../registry/employees/dana.toml) |
| Automation | Email Intake & Routing | [`email-intake-routing.toml`](../../../registry/automations/email-intake-routing.toml) |

Dana follows the activation rule of [Phase 9](phase-09-router-advisor.md) slice 9.7; the automation the rule of [Phase 5](phase-05-orders-ingest.md).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 14.1 | Inbound pipeline: webhook, inbox, fetch, attachments, quarantine | Forged, replayed and oversized mail rejected; attachments stored safely |
| 14.2 | Email Intake & Routing: threads, tickets, customer linking by order reference, rule-based routing | A reply lands in the right thread; no cross-shop linking; replaying a webhook creates no second ticket |
| 14.3 | Quarantined extractor → validated ticket schema | Injection corpus in emails causes no tool call and no schema escape |
| 14.4 | Dana's drafts and approval-to-send | Nothing is sent without approval; sends are idempotent and audited; Dana never promises refunds or compensation |
| 14.5 | Etsy assisted replies | Draft copy-to-clipboard and "mark as sent" with audit |
| 14.6 | Support UI: inbox, ticket view, SLA | Masked PII by default; role-limited access |
| 14.7 | Dana activated | Sheet defined, evaluation passed, autonomy approved, routing measured (including the data-class limit of her policy) |

## Test plan

Webhook security tests; injection corpus; automation replay, failure and recovery tests; Dana's evaluation set (classification, reply quality, refusal of unsafe promises); the routing benchmark if the advisor is used; real inbound mail end to end.

## Security checklist

- [ ] FR-EML-03 controls; LINDDUN review for buyer messages; retention job for DC2 data.
- [ ] Rule of Two: the extractor reads untrusted mail with no tools; Dana never sees raw HTML.
- [ ] Instructions inside customer messages are data; Dana's grants cannot change from message content.

## Documentation

`docs/features/email-inbound/`, `docs/features/support/`, `docs/features/employees/dana.md`; the automation specification completed.

## Exit gate

- [ ] Real inbound mail handled end to end; injection corpus passes; owner approves the first sent reply.
- [ ] Dana and Email Intake & Routing are `ready` and approved.

## Rollback

Dana's and the automation's kill switches; inbound mail keeps arriving in the provider and can be re-ingested idempotently.

## Risks

| Risk | Mitigation |
|---|---|
| Spam and abuse through the inbound addresses | Provider spam filtering, attachment policy, rate limits per sender |
| A drafted reply makes an unsafe promise | Approval before sending; evaluation cases for refusals; Noa samples sent replies |

## Cost impact

Resend Pro (about $20/month) once inbound is on, since inbound counts toward the sending quota; Dana's cost per resolved request measured.
