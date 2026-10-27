-- A waiting execution names the action it waits for (AUD-03, Codex on #175): the gateway's
-- action execution whose approval or hand-off it waits on. A decision resumes or ends only the
-- wait it was taken for, so a replayed `approvals.decided.v1` never resumes a later wait. The
-- engine's optimistic update checks it with the state and the attempts.
--
-- Safe on the live database: one nullable column and no backfill. No decision can reach a row
-- waiting today (the worker's gateway is `NoApprovals` until slice 7.11, so nothing approves),
-- and a resume that names no action (a finished Human task) is unchanged. No new grant: the
-- worker already updates the table, and signed-in users keep their column-level select, which
-- does not include it.

alter table app.automation_executions add column waiting_for uuid
  constraint automation_executions_waiting_for_check check (
    waiting_for is null or state = 'waiting'
  );
