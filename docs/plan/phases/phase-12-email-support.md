# Phase 12 — Email and customer service

| | |
|---|---|
| **Milestone** | — |
| **Repositories** | both |
| **Size** | M |
| **Depends on** | Phases 7 and 8 |
| **Requirements** | FR-AGT-06, FR-EML-02, FR-EML-03, FR-MKT-02, FR-SVC-01, FR-SVC-02, FR-SVC-03 |
| **Status** | Not started — acceptance criteria refined at kickoff |

## Progress

- [ ] 12.1 Inbound pipeline: webhook, inbox, fetch, attachments, quarantine
- [ ] 12.2 Threads, tickets, customer linking by order reference
- [ ] 12.3 Quarantined extractor → validated ticket schema
- [ ] 12.4 Customer Service drafts and approval-to-send
- [ ] 12.5 Etsy assisted replies
- [ ] 12.6 Support UI: inbox, ticket view, SLA

## Goal

`support@` and `info@office.ht-co.app` become a shared inbox inside HTCO: inbound mail is verified, quarantined, threaded and linked to orders and customers; agents draft replies that people approve. Etsy buyer conversations get assisted replies (drafted in HTCO, sent by a person in Etsy), because Etsy offers no messaging API.

## Scope

**In:** MX for `office.ht-co.app` to Resend inbound (addresses `support@`, `info@` and `security@`); `email.received` webhook (signed, idempotent), content fetch, attachment copy within the provider's one-hour URL window, attachment policy and scanning, HTML sandboxing with remote content off; threads and tickets; quarantined extractor (no tools) that turns an email into a validated schema; Customer Service agent drafting replies (L2: sending needs approval); assisted-reply workflow for Etsy conversations with a "sent manually" record; consent ledger; retention for messages (data class DC2).

**Out:** per-agent addresses (Phase 13+), marketing email.

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 12.1 | Inbound pipeline: webhook, inbox, fetch, attachments, quarantine | Forged, replayed and oversized mail rejected; attachments stored safely |
| 12.2 | Threads, tickets, customer linking by order reference | A reply lands in the right thread; no cross-shop linking |
| 12.3 | Quarantined extractor → validated ticket schema | Injection corpus in emails causes no tool call and no schema escape |
| 12.4 | Customer Service drafts and approval-to-send | Nothing is sent without approval; sends are idempotent and audited |
| 12.5 | Etsy assisted replies | Draft copy-to-clipboard and "mark as sent" with audit |
| 12.6 | Support UI: inbox, ticket view, SLA | Masked PII by default; role-limited access |

## Security checklist

- [ ] FR-EML-03 controls; LINDDUN review for buyer messages; retention job for DC2 data.
- [ ] Rule of Two: the extractor reads untrusted mail with no tools; the drafting agent never sees raw HTML.

## Exit gate

- [ ] Real inbound mail handled end to end; injection corpus passes; owner approves the first sent reply.

## Cost impact

Resend Pro (about $20/month) once inbound is on, since inbound counts toward the sending quota.
