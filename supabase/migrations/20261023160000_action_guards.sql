-- The Action Gateway's guards (slice 5.15, execution-engine.md §4 step 6 and §9): state shared by
-- every worker.
--
-- - jobs.rate_windows: calls spent per key in aligned windows (each UTC second and day); an upsert
--   adds a call only while the window stays inside its budget (etsy:app: QPS and QPD).
-- - jobs.circuit_breakers: one breaker per key (etsy:api, etsy:web, later browser:{shop}).
-- - jobs.concurrency_slots: leased slots, for example one write per shop at a time.
--
-- No tenant data: keys name a channel or a shop ID only. Reachable by the worker role (and the
-- owner role the API logs in with), never by users.
-- app.action_executions.retry_at keeps when a deferred action may run again.

create table jobs.rate_windows (
  key text not null constraint rate_windows_key_check check (char_length(key) between 1 and 200),
  scope text not null constraint rate_windows_scope_check check (scope in ('second', 'day')),
  starts_at timestamptz not null,
  used integer not null constraint rate_windows_used_check check (used >= 0),
  primary key (key, scope, starts_at)
);

create table jobs.circuit_breakers (
  key text primary key
    constraint circuit_breakers_key_check check (char_length(key) between 1 and 200),
  state text not null default 'closed'
    constraint circuit_breakers_state_check check (state in ('closed', 'open', 'half_open')),
  failures integer not null default 0
    constraint circuit_breakers_failures_check check (failures >= 0),
  window_started_at timestamptz,
  opened_at timestamptz,
  probe_at timestamptz,
  updated_at timestamptz not null default now(),
  constraint circuit_breakers_open_check check (state = 'closed' or opened_at is not null)
);

create table jobs.concurrency_slots (
  key text not null
    constraint concurrency_slots_key_check check (char_length(key) between 1 and 200),
  slot integer not null constraint concurrency_slots_slot_check check (slot >= 0),
  holder uuid not null,
  lease_until timestamptz not null,
  primary key (key, slot)
);
create index concurrency_slots_holder_idx on jobs.concurrency_slots (key, holder);

alter table jobs.rate_windows enable row level security;
alter table jobs.circuit_breakers enable row level security;
alter table jobs.concurrency_slots enable row level security;
revoke all on jobs.rate_windows, jobs.circuit_breakers, jobs.concurrency_slots
  from public, anon, authenticated, service_role;
grant select, insert, update, delete
  on jobs.rate_windows, jobs.circuit_breakers, jobs.concurrency_slots to htco_worker;
create policy rate_windows_worker on jobs.rate_windows for all to htco_worker
  using (true) with check (true);
create policy circuit_breakers_worker on jobs.circuit_breakers for all to htco_worker
  using (true) with check (true);
create policy concurrency_slots_worker on jobs.concurrency_slots for all to htco_worker
  using (true) with check (true);

alter table app.action_executions add column retry_at timestamptz;
grant select (retry_at) on app.action_executions to authenticated;
