# Activity records — Overview

| | |
|---|---|
| **Slug** | `activity-records` |
| **Phase / slices** | Phase 7: 7.7 (the store) and 7.8 (the query API) are built; the web view of 7.8 is next |
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
| AC-7.7.3 | A record of organization A is invisible to B; a shop record is invisible without that shop; an organization-wide record is for Owners and Admins only (pgTAP) | Done |
| AC-7.7.4 | No column can hold a prompt, model output or raw payload: summaries come from code templates, the actor, correlation and causation ids are identifiers, and a test fails on free text | Done |
| AC-7.8.1 | "What did `automation:etsy-order-intake` do today" returns that day's records in the user's time zone, nothing from shops the user is not granted | API done (`GET /v1/activity?actor=…&day=…&timezone=…`) |
| AC-7.8.2 | "Which automations failed" returns every failed execution in the range, linked to its execution and dead-letter state | Partly done: `actor_kind=automation&outcome=failed` with the range; the link to the execution and its dead-letter state waits for the automation writer (an execution ID on the record) |
| AC-7.8.3 | A task's trail shows every record linked by correlation ID in causal order, across actors | API done (`GET /v1/tasks/{id}/activity`, oldest first) |
| AC-7.8.4 | The view states its data source and freshness; an empty result says "no recorded activity" | API done (`source`, `read_at`, `empty_reason`); the web view follows |

## The query (slice 7.8)

`GET /v1/activity` filters by actor (`employee:shon`), actor kind, shop, task, kind, outcome and a range (`since`/`until`) or a `day` in the caller's `timezone`, newest first in cursor pages of at most 200. "View agent runs" decides who reads: Owner and Admin the whole organization, Operations and Developer their granted shops' records only (no organization-wide ones), as the RLS policy. The shop scope is in the SQL, before the limit. `GET /v1/tasks/{id}/activity` is a task's trail: its records and every record sharing their correlation IDs, oldest first, with the same shop scope, in pages of `limit` (at most 200); `next_cursor` continues after the last record, so a long trail is never cut (AC-7.8.3, quill2 on #200). Open: the department filter (from the registry's employees), saved queries and the web view "Activity" in `htco-clients` (ER-UX-07).

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
