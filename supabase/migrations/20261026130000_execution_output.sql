-- A completed execution keeps its handler output (#127): per-run identifiers and counts, for
-- example Tracking Sync's `{"receipts": 2, "recorded": 1}`. The worker writes only what the
-- automation's typed output model allows (`TypedOutputs`: closed keys, integers, codes, flags,
-- UUIDs), so it is never buyer data. The
-- API shows it to the Owner and Admins; a signed-in user never reads it directly.
--
-- Safe on the live database: one nullable column and a column-level grant.

alter table app.automation_executions add column output jsonb
  constraint automation_executions_output_check check (
    output is null or (jsonb_typeof(output) = 'object' and pg_column_size(output) <= 8192)
  );

-- Signed-in users keep reading every other column (RLS unchanged); not the output.
revoke select on app.automation_executions from authenticated;
grant select (
  id, organization_id, shop_id, automation_id, automation_version, idempotency_key, trigger,
  actor_kind, actor_id, state, attempts, correlation_id, causation_id, last_error, payload,
  created_at, updated_at
) on app.automation_executions to authenticated;
