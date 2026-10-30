-- One resumer per action execution (Codex post-merge review, owner gate v4, P2).
--
-- A resumable execution (`waiting_approval`, `failed`, `deferred`) was read with a plain select
-- after the idempotency conflict, so two workers resubmitting the same key both called the
-- channel (an extra Etsy request), and the loser's record then hit the final-state trigger. Now:
--
-- - `generation`: raised by the gateway's compare-and-set resumption (`ActionStore.resume`);
--   every record must match the generation its caller holds, so a loser never writes.
-- - `resuming_until`: the winner's lease (the action's timeout and the slot margin); while it
--   holds, another caller answers `in_progress`. A record releases it; a crashed winner's lease
--   runs out and the row may be resumed again.
--
-- Safe on the live database: two columns with constant defaults (no table rewrite); the existing
-- grants and policies already cover them (the API's login role and the worker's job organization).

alter table app.action_executions
  add column generation integer not null default 0
    constraint action_executions_generation_check check (generation >= 0),
  add column resuming_until timestamptz;
