# Phase 14 — Email and customer service

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | [Phase 10](phase-10-supervision.md) (can run in parallel with Phases 11–13); [Phase 8A](phase-08a-browser-infrastructure.md) (browser engine) and [Phase 8B](phase-08b-authenticator.md) (authenticator) for delivery A; [Phase 9](phase-09-router-advisor.md) slice 9.10 (knowledge base and retrieval, [ADR-0024](../../adr/0024-knowledge-base-and-retrieval.md)) for Dana's drafts |
| **Requirements** | FR-AGT-06, FR-AGT-09, FR-AUT-05, FR-EML-02, FR-EML-03, FR-MKT-02, FR-SVC-01, FR-SVC-02, FR-SVC-03 |
| **Status** | Not started |

Rolling wave: this phase is planned at the level of scope, slices and gates; acceptance criteria are refined at kickoff. The "key acceptance" column below is the minimum.

## Progress

- [ ] 14.1 Inbound pipeline: webhook, inbox, fetch, attachments, quarantine (generalizes the quote mailbox of slice 7.14)
- [ ] 14.2 Email Intake & Routing automation: threads, tickets, customer linking
- [ ] 14.3 Quarantined extractor to a validated ticket schema
- [ ] 14.4 Dana's drafts and approval-to-send
- [ ] 14.5 Etsy assisted replies
- [ ] 14.6 Support UI: inbox, ticket view, SLA
- [ ] 14.7 Dana activated

## Architecture upgrade (2026-10-23)

Changes from [ADR-0019](../../adr/0019-unified-execution-engine-and-action-gateway.md) (Action Gateway, four channels), [ADR-0022](../../adr/0022-etsy-write-tools-and-browser-scope.md) (Etsy write tools and browser scope), [ADR-0020](../../adr/0020-per-shop-browser-environment.md) and [ADR-0021](../../adr/0021-authenticator-service.md):

- **Delivery A runs on the central browser engine.** "An AI agent sends approved replies in an isolated browser session" is the shop's Browser Environment (Phase 8A), its Browser Controller and lock, the shared authenticator (Phase 8B) for sign-in, and the challenge handler that hands off to a person. It is not a separate browser.
- **Scope rows:** reading messages is B-04, sending an approved reply B-05, reading cases B-06, and replying to reviews B-07. Each is off until the owner records its scope, and B-05 and B-07 also need the written confirmation (recommended in ADR-0022).
- **The send itself** is a deterministic Playwright recipe that types the exact approved text. Dana drafts in HTCO; she never drives the browser to compose.
- **Delivery B** (assisted reply through the owner's own browser) stays the fallback, and is the default whenever a flag is off.
- **Etsy's notification emails** stay the primary signal for new messages and cases, with no browser needed for detection.

## Goal

`support@` and `info@office.ht-co.app` become a shared inbox inside HTCO: the **Email Intake & Routing** automation verifies, quarantines, threads and links inbound mail to orders and customers, and routes it by rules (the Decision Advisor only when rules are inconclusive and its benchmark passed); **Dana** (Customer Service) classifies requests, retrieves order context, and drafts replies that people approve. Etsy buyer conversations get assisted replies (drafted in HTCO, sent by a person in Etsy), because Etsy offers no messaging API.

## Scope

**Etsy buyer messages (owner, 2026-10-15, revised).** Etsy has no messages API ([capabilities](../../architecture/etsy-api-capabilities.md)). One shared pipeline, two ways to deliver the reply.

- **Shared pipeline:**
  1. Etsy emails a notification for every buyer message. HTCO receives it, either forwarded from Gmail or through the Gmail API (read-only scope).
  2. HTCO extracts the buyer's name, the conversation link and the message text.
  3. It matches the message to the order through the official API.
  4. Dana drafts a reply under the shop's policies.
  5. A person approves the draft in the dashboard.
- **Delivery A, tried first (the owner's decision, one pilot shop):** an AI agent sends approved replies itself in an isolated browser session.
  - Human pace, and no disguise of any kind: no proxies, no fingerprint changes, no CAPTCHA solving.
  - Off by default, per shop, with a kill switch.
  - This is automated use of etsy.com, which Etsy's terms restrict. The owner accepts that risk per shop, and it is recorded as a decision.
  - If it causes any trouble (warnings, challenges, a restriction), it is switched off and the shop moves to delivery B.
- **Delivery B, assisted reply:**
  - The dashboard's "Reply in Etsy" button opens the conversation on etsy.com in the owner's own browser.
  - A small HTCO Chrome extension pastes the approved reply into the message box, and the owner clicks Send.
  - Nothing signs in to Etsy from our servers, and no automated browser or automatic send is involved.
  - The extension fills the box only on the owner's click. It holds no Etsy credentials and reads nothing else from the page.


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
| 14.4 | Dana's drafts and approval-to-send. She grounds her drafts in structured tools first (order, listing, tracking), then in `knowledge.search` (slice 9.10: shop policies, FAQ, listings, products, approved replies). Each approved and sent reply is ingested as an `approved_reply` after redaction | Nothing is sent without approval; sends are idempotent and audited; Dana never promises refunds or compensation; every fact in a draft cites a tool result or a chunk ID; a buyer's own message is never ingested into the knowledge base |
| 14.5 | Etsy assisted replies | Draft copy-to-clipboard and "mark as sent" with audit |
| 14.6 | Support UI: inbox, ticket view, SLA | Masked PII by default; role-limited access |
| 14.7 | Dana activated | Sheet defined, evaluation passed, autonomy approved, routing measured (including the data-class limit of her policy); her knowledge policy is set, and the 9.10 retrieval eval passes on her shop's knowledge (AC-9.10.11) |

## Test plan

Webhook security tests; injection corpus; automation replay, failure and recovery tests; Dana's evaluation set (classification, reply quality, refusal of unsafe promises); the routing benchmark if the advisor is used; real inbound mail end to end.

## Security checklist

- [ ] FR-EML-03 controls; LINDDUN review for buyer messages; retention job for DC2 data.
- [ ] Rule of Two: the extractor reads untrusted mail with no tools; Dana never sees raw HTML.
- [ ] Instructions inside customer messages are data; Dana's grants cannot change from message content.
- [ ] Retrieved knowledge is data too (AC-9.10.10). Only redacted, approved replies enter the knowledge base, never a buyer's message (AC-9.10.2, AC-9.10.3).

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
