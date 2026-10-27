# Features

Every feature has its own folder here, created from [`_template/`](_template/) at the start of its first slice and kept true in every later pull request. The [`feature-docs`](../../.claude/skills/feature-docs/SKILL.md) skill explains what goes in each file.

| File | Answers |
|---|---|
| `overview.md` | What does it do, for whom, and how do we know it works (acceptance criteria)? |
| `design.md` | How is it built: layers, contracts, data, flows, decisions? |
| `tests.md` | Which tests cover which criteria, and what did real verification show? |
| `security.md` | What can go wrong, what stops it, which ASVS requirements are met? |
| `operations.md` | How is it configured, monitored, fixed and rolled back? |

## Index

| Feature | Phase | Status |
|---|---|---|
| [platform-skeleton](platform-skeleton/overview.md) | 1 | In progress |
| [identity-access](identity-access/overview.md) | 2 | In progress |
| [living-office](living-office/overview.md) | 2 (foundation), 5–19 | Foundation complete |
| [etsy-integration](etsy-integration/overview.md) | 4 | In progress (4.1 to 4.5 done at the platform; 4.7 disconnect done, restore rehearsal pending) |
| [job-queue](job-queue/overview.md) | 5 | 5.1 and 5.2 built; worker deployment pending the owner |
| [automation-engine](automation-engine/overview.md) | 5 | 5.10 built; first automations in 5.11 |
| [action-gateway](action-gateway/overview.md) | 5 | 5.12 built; channels in 5.13 |
| [orders-ingest](orders-ingest/overview.md) | 5 | 5.3 webhooks, 5.4 receipt ingest and 5.5 reconciliation built (off behind flags and schedules until 5.11) |
| [secret-broker](secret-broker/overview.md) | 5, 8B | 5.17 contracts and in-memory implementations built; tables and UI in 8B |
| [fulfillment-monitoring](fulfillment-monitoring/overview.md) | 6 | 6.1 tracking detection and 6.2 SLA inspector built (draft until 6.7) |
| [incidents](incidents/overview.md) | 6 | 6.3 incident machine and deduplication built; the incident center API with 6.4 |
| [notifications](notifications/overview.md) | 6 | 6.4 built; email on the log adapter until the owner's Resend DNS |
| [activity-records](activity-records/overview.md) | 7 | 7.7 store, writer and templates built; the use cases start writing as their locks clear |
| [browser-environments](browser-environments/overview.md) | 5, 8A | 5.16 contracts, table and state machine built; the browser in 8A |
