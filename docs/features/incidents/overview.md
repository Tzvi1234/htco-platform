# Incidents — Overview

| | |
|---|---|
| **Slug** | `incidents` |
| **Phase / slices** | Phase 6 — 6.3 (the incident aggregate, machine and deduplication) built; 6.4 (notifications) and 6.5 (the incident center UI) follow |
| **Requirements** | [FR-INC-02, FR-INC-03, FR-INC-05](../../product/PRD.md) |
| **Status** | Built; opened only by Incident Detection & Escalation, which is `draft` until slice 6.7 |
| **Last updated** | 2026-10-25 |

## What and why

An incident is one problem with one resource: an order past its SLA window without tracking, a drift reconciliation found, a tracking mismatch of the relay, an automation that gave up. There is **exactly one open incident per root cause and resource**, so the owner hears about a problem once. An incident resolves **only on verified evidence** (tracking now present in Etsy, the order cancelled, the drift cleared), never on someone's statement.

## The machine (FR-INC-02)

`DETECTED → TRIAGED → INVESTIGATING → REMEDIATION_PROPOSED → (AUTO_REMEDIATING | APPROVAL_REQUIRED | DEVELOPER_REQUIRED) → VERIFYING → RESOLVED`, and `ESCALATED` from every open state. Evidence moves any open incident through `VERIFYING` to `RESOLVED`; a failed verification goes back to `INVESTIGATING`. `RESOLVED` is reached only from `VERIFYING`, with evidence; the database checks it again.

Severity: `low`, `medium`, `high`, `critical`. Defaults: missing tracking `medium`, drift `low`, tracking mismatch and dead letters `high`. An incident nobody took past investigating escalates after 7 days (low), 3 days (medium), 24 hours (high) or 4 hours (critical).

## Acceptance criteria

| ID | Criterion |
|---|---|
| AC-6.3.1 | Two breaches for the same order and cause produce one incident. |
| AC-6.3.2 | When tracking appears, the incident resolves with the evidence recorded; without evidence, it never resolves. |
| AC-6.3.3 | Every illegal state transition is rejected (state-machine tests). |
| AC-6.3.4 | A drift recorded by reconciliation (Phase 5) opens one incident linked to the order. |

AC-6.3.4 is built on the incident side (the `drift` payload of Incident Detection & Escalation and `drift_detection`); its trigger from `app.sync_drifts` is wired after slice 5.5 (PR #75) merges.
