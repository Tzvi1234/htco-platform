# Activity records — Operations

The migration `20261027150000_activity_records.sql` is additive and safe on the live database. There are no settings.

- **Partitions:** they exist through 2027-12. Before then, create the next months, for example `select private.create_activity_partition(date '2028-01-01');` (owner role). A monthly maintenance job will do this automatically; it is a follow-up. Until a month has its partition, its rows land in `app.activity_records_default`.
  - Creating a month's partition fails if the default partition already holds rows of that month. Create the partitions ahead of time.
- **Finding a trail:** use `(organization_id, correlation_id)`, or `(organization_id, actor_kind, actor_id, occurred_at desc)`. The query API arrives in 7.8.
- **Rollback:** the table is additive, and nothing reads it yet, so a code rollback leaves it unused.
