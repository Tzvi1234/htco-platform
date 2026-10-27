# Activity records — Overview

| | |
|---|---|
| **Slug** | `activity-records` |
| **Phase / slices** | Phase 7: 7.7 (the store) is built; 7.8 (the query API and view) is next |
| **Requirements** | [ADR-0012](../../adr/0012-living-office-organization.md) rule 4, ER-AI-16, ER-AI-09 |
| **Status** | The store, the writer and the summary templates are built. The use cases start writing as their areas' locks clear (see the follow-ups below). |
| **Last updated** | 2026-10-27 |

## What and why

Every meaningful operation by any actor leaves one record in `app.activity_records`. That record is the one trail that the office, supervision and optimization read. A record states:

- who acted, what they did and the outcome;
- the organization, shop and task;
- correlation and causation IDs;
- a short business summary.

The summary is always a code template filled with identifiers. It is never model reasoning, a prompt or a raw payload.

## Acceptance criteria

| ID | Criterion | Status |
|---|---|---|
| AC-7.7.1 | Every task transition, approval decision, incident transition and automation execution writes exactly one record | Partly done. The writer is ready, and the writers in the use cases are follow-ups (below) |
| AC-7.7.2 | The application role cannot update or delete; an over-long summary, a missing correlation ID or a naive timestamp is rejected | Done |
| AC-7.7.3 | A record of organization A is invisible to B; a shop record is invisible without that shop (pgTAP) | Done |
| AC-7.7.4 | No column can hold a prompt, model output or raw payload: summaries come from code templates, and a test fails on free text | Done |

## Follow-ups (AC-7.7.1)

Each one calls `append_activity` in its own transaction, with a template from `TEMPLATES`:

- **Task transitions:** `TaskService` in #181 (7.1), template `task.moved`.
- **Approval decisions:** `approvals.py` in #175 (AUD-03) and slice 7.3, template `approval.decided`.
- **Incident transitions:** `IncidentService` in `incidents.py`. #173 and #174 are open on that module. Templates `incident.opened` and `incident.moved`.
- **Automation executions:** `automation/durable.py` (lyra-113's lock), template `automation.finished`.
- **Notifications:** lyra-113 holds them for 7.6. Template `notification.sent`.
- **Policy decisions** (#189): template `policy.decided`.
- **The coverage test of AC-7.7.1** walks these use cases once they write.
- **A monthly maintenance job** creates partitions ahead (they exist through 2027-12).
- **A foreign key on `task_id`** follows once `app.tasks` is on main.
