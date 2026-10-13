# Phase 13 — Workforce, Office and Owner Assistant

| | |
|---|---|
| **Milestone** | **M5 — The office** |
| **Repositories** | both |
| **Size** | L |
| **Depends on** | Phase 8 (and the workflows built since) |
| **Requirements** | FR-ADM-04, FR-AGT-06, FR-AGT-08, FR-EML-04, FR-MKT-01, FR-NTF-02, FR-OFC-01, FR-OFC-02, FR-OFC-03, FR-OFC-04, FR-OFC-05 |
| **Status** | Not started — acceptance criteria refined at kickoff |

## Progress

- [ ] 13.1 Live-update spike and ADR
- [ ] 13.2 Agent states from verified events; offline detection from heartbeats
- [ ] 13.3 Office v1 rooms grid and list view
- [ ] 13.4 Conversations and action receipts
- [ ] 13.5 Owner Assistant chat with inline approvals
- [ ] 13.6 Optional Hermes channel through the Action API
- [ ] 13.7 Web push notifications with consent (FR-NTF-02)
- [ ] 13.8 Developer console on the web (FR-ADM-04): runs, traces, dead-letter queue, by role

## Goal

The owner sees the AI office as it really is: departments, agents and their verified states, conversations made of business messages and action receipts, and a chat with the General Manager or any agent, with approvals inline.

## Scope

**In:** live updates transport (decided by a spike: server-sent events with reconnects through DigitalOcean's request timeout, or Supabase Realtime private channels, A-12); Office v1 (rooms grid with an accessible list view; no invented activity; read-only when the worker is offline); agent detail (task, messages, outputs, memory references, tools, costs); conversations per task, shop and incident; Owner Assistant chat through the Action API; optional Hermes bridge (D-10); General Manager agent coordinating departments.

**Out:** the animated office canvas (a later iteration once v1 proves useful).

## Slices (outline)

| Slice | Content | Key acceptance |
|---|---|---|
| 13.1 | Live-update spike and ADR | Reconnect and replay with a cursor; no state derived only from the stream |
| 13.2 | Agent states from verified events; offline detection from heartbeats | Worker offline → office read-only, no agent shown as running |
| 13.3 | Office v1 rooms grid and list view | List view carries the same information; reduced motion respected |
| 13.4 | Conversations and action receipts | Messages are business events; no hidden reasoning shown |
| 13.5 | Owner Assistant chat with inline approvals | Approvals from chat follow the same server checks |
| 13.6 | Optional Hermes channel through the Action API | Limited action set; no database access; Hermes can notify and deep-link to approvals but cannot decide them |
| 13.7 | Web push notifications with consent (FR-NTF-02) | A push opens the exact item after sign-in; subscriptions are removed on logout |
| 13.8 | Developer console on the web (FR-ADM-04): runs, traces, dead-letter queue, by role | Traces masked; actions limited to safe, audited operations |

## Exit gate

- [ ] Office reflects real runs during a live demo; offline mode verified; owner chat completes an approval.
