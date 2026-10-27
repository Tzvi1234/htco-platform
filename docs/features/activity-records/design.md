# Activity records — Design

| Layer | Module | Role |
|---|---|---|
| Domain | `htco_domain.activity` | `ActivityRecord`, `ActivityKind` and `ActivityOutcome`, with their rules: an aware timestamp, a correlation ID, and a summary of 1 to 500 characters |
| Application | `htco_application.activity` | `TEMPLATES`, `summarize`, `Summary`, `new_activity` → `ActivityEntry`, and the `ActivityWriter` port |
| Persistence | `htco_persistence.activity` | `append_activity(connection, entry)` in the caller's transaction, and `PostgresActivityLog` for a write in its own transaction |

## Summaries

`summarize(template, **params)` looks up a code template, for example `"incident.opened": "Opened incident {incident} for receipt {receipt}"`. It requires exactly that template's parameters, and each value must be an identifier: `[A-Za-z0-9][A-Za-z0-9_.:#/-]{0,63}`, with no spaces. Only `summarize` can build a `Summary`, and `ActivityEntry`, `new_activity` and `append_activity` accept nothing else. So a writer cannot pass free text from a model, a buyer or an external source. The row stores the template key next to the text, so the 7.8 view can render the statement in Hebrew or English.

## The table

- **Partitioning:** `app.activity_records` is partitioned by range on `occurred_at`. The primary key is `(id, occurred_at)`. Partitions are named `activity_records_YYYY_MM` and exist from 2026-10 to 2027-12. A default partition catches any other month, so a write never fails for lack of a partition. `private.create_activity_partition(month)` creates a month idempotently, with RLS on and no direct grants.
- **Append-only:** a trigger refuses update and delete for every role, including the table owner. Rows are removed only when their organization or shop is removed, through a cascade at trigger depth > 0.
- **Indexes:** by organization and time; by shop; by actor; and by correlation ID, for 7.8's filters and a task's trail.
- **`task_id`:** no foreign key yet, because `app.tasks` is in #181.
