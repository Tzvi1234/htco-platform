# Notifications — Overview

| | |
|---|---|
| **Slug** | `notifications` |
| **Phase / slices** | Phase 6 — 6.4 (in-app notifications, email, security notices) built; the bell and the incident center UI are slice 6.5 (clients) |
| **Requirements** | [FR-NTF-01, FR-EML-01](../../product/PRD.md) |
| **Status** | Built. Email goes to the **log adapter** (nothing is sent) until the owner verifies Resend's DNS for `office.ht-co.app` and sets `HTCO_EMAIL_PROVIDER=resend` |
| **Last updated** | 2026-10-25 |

## What and why

People hear about what needs them once, in the office and by email: an incident opened or escalated (6.7), an alert (6.9), a tracking number to copy (6.10), and security events. Every notification carries a kind and identifiers only (the receipt number, never a buyer's name, address or tracking code); the words are rendered in the reader's language, Hebrew or English.

**One path (architecture audit P2-2).** Every source reaches people the same way: incidents (`incidents.opened.v1`), alerts and security notices (`notifications.requested.v1`, or a `NotificationRequest` in the worker) become `notifications` rows and `email.send` outbox messages. There is no second alert table.

## Acceptance criteria

| ID | Criterion |
|---|---|
| AC-6.4.1 | One incident produces one email even if the delivery job runs twice. |
| AC-6.4.2 | Email content contains no buyer PII beyond the receipt number. |
| AC-6.4.3 | Resend failures retry with backoff; permanent failures are visible in the admin view. |
| AC-6.4.4 | A sign-in from a new device sends one email to that user; MFA changes and shop connections notify the owner. |

AC-6.4.3's admin view is `GET /v1/operations/email-failures` (Owner and Admins, like the rest of Admin > Health): the failed (or, with `status=skipped`, skipped) rows of `app.email_deliveries`, newest first, with their kind, reason, time and the shop, incident and order they were about (recorded since migration `20261028120000`). The recipient is a member ID and an address the database masks (`o***@e***.test`, `app.admin_member_email_masked`); the full address never reaches the API. Dead `email.send` outbox messages show in the queue health view of 5.9.
