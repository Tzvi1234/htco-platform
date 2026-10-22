-- Inbox, outbox and Idempotency-Key (slice 5.2, SOFTWARE_DESIGN §8.3, ER-REL-01, ER-REL-02).
--
-- `jobs.inbox` keeps every external event once: unique by `(source, event_id)`. Recording an event
-- and enqueueing its processing job happen in one transaction, so an event delivered twice gives
-- one row and one job (AC-5.2.1). The payload is what the source sent (at most 64 KB) with its
-- SHA-256; webhook slices decide what they keep in it.
--
-- `jobs.outbox` keeps every external side effect as an intent written in the same transaction as
-- the business change, unique by its idempotency key, with its delivery status, attempts and next
-- attempt time. A delivery job (`outbox.deliver`) is enqueued in that transaction too, so a crash
-- after the business commit still delivers exactly once after restart (AC-5.2.2). The worker reaches
-- an organization's outbox rows only inside that organization's jobs.
--
-- `private.idempotency_keys` stores, for 24 hours, the hash of an unsafe API request sent with an
-- `Idempotency-Key` and its response, so a retry gets the stored response and has no second effect;
-- the same key with another request is refused (AC-5.2.3). Only the API's login role uses it.

create table jobs.inbox (
  id uuid primary key,
  source text not null constraint inbox_source_check check (source ~ '^[a-z][a-z0-9_]{0,31}$'),
  event_id text not null
    constraint inbox_event_id_check check (char_length(event_id) between 1 and 200),
  event_type text not null
    constraint inbox_event_type_check check (char_length(event_type) between 1 and 100),
  organization_id uuid references app.organizations (id) on delete cascade,
  shop_id uuid,
  payload jsonb not null constraint inbox_payload_check check (
    jsonb_typeof(payload) = 'object' and pg_column_size(payload) <= 65536
  ),
  payload_hash bytea not null
    constraint inbox_payload_hash_check check (octet_length(payload_hash) = 32),
  received_at timestamptz not null,
  processed_at timestamptz,
  job_id uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint inbox_source_event_id_key unique (source, event_id),
  constraint inbox_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint inbox_shop_needs_organization_check check (
    shop_id is null or organization_id is not null
  )
);
create index inbox_unprocessed_idx on jobs.inbox (received_at) where processed_at is null;
create index inbox_organization_id_idx on jobs.inbox (organization_id);
create index inbox_shop_id_idx on jobs.inbox (shop_id);

create trigger inbox_set_updated_at before update on jobs.inbox
  for each row execute function private.set_updated_at();

create table jobs.outbox (
  id uuid primary key,
  organization_id uuid not null references app.organizations (id) on delete cascade,
  shop_id uuid,
  destination text not null constraint outbox_destination_check check (
    char_length(destination) <= 64 and destination ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'
  ),
  idempotency_key text not null
    constraint outbox_idempotency_key_check check (char_length(idempotency_key) between 1 and 200),
  -- IDs and parameters of the effect; never tokens.
  payload jsonb not null constraint outbox_payload_check check (
    jsonb_typeof(payload) = 'object' and pg_column_size(payload) <= 16384
  ),
  status text not null default 'pending' constraint outbox_status_check check (
    status in ('pending', 'delivered', 'dead')
  ),
  attempts integer not null default 0 constraint outbox_attempts_check check (attempts >= 0),
  next_attempt_at timestamptz not null,
  last_error text constraint outbox_last_error_check check (char_length(last_error) <= 500),
  delivered_at timestamptz,
  -- The gateway's action this effect belongs to (ADR-0019; `action_executions` arrives in slice
  -- 5.12, the reference with it), and the request or trigger that started it.
  action_execution_id uuid,
  correlation_id text
    constraint outbox_correlation_id_check check (char_length(correlation_id) between 1 and 200),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint outbox_idempotency_key_key unique (idempotency_key),
  constraint outbox_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint outbox_delivered_at_check check ((status = 'delivered') = (delivered_at is not null))
);
create index outbox_pending_idx on jobs.outbox (next_attempt_at) where status = 'pending';
create index outbox_organization_id_idx on jobs.outbox (organization_id);
create index outbox_shop_id_idx on jobs.outbox (shop_id);
create index outbox_action_execution_id_idx on jobs.outbox (action_execution_id)
  where action_execution_id is not null;

create trigger outbox_set_updated_at before update on jobs.outbox
  for each row execute function private.set_updated_at();

-- The outbox row follows its delivery job: the job's next run time, and `dead` when the job
-- dies, also after a crash on the last attempt, when the delivery code never ran (Codex review
-- of #55). Security definer: the job may be released outside any organization's scope.
create function private.sync_outbox_with_job() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  update jobs.outbox
  set next_attempt_at = new.run_at,
      status = case when new.state = 'dead' then 'dead' else status end,
      last_error = case when new.state = 'dead' then coalesce(new.last_error, last_error)
                   else last_error end
  where id = (new.payload ->> 'outbox_id')::uuid and status = 'pending';
  return null;
end;
$$;
revoke all on function private.sync_outbox_with_job() from public;
create trigger jobs_sync_outbox after update of state, run_at on jobs.jobs
  for each row
  when (new.kind = 'outbox.deliver'
        and (new.state is distinct from old.state or new.run_at is distinct from old.run_at))
  execute function private.sync_outbox_with_job();

alter table jobs.inbox enable row level security;
alter table jobs.outbox enable row level security;
revoke all on jobs.inbox, jobs.outbox from public, anon, authenticated, service_role;
-- Inbox events may arrive before their organization is known: like the queue, cross-tenant.
grant select, insert, update on jobs.inbox to htco_worker;
create policy inbox_worker on jobs.inbox for all to htco_worker using (true) with check (true);
-- Outbox rows: only inside the organization's jobs.
grant select, insert, update on jobs.outbox to htco_worker;
create policy outbox_worker on jobs.outbox for all to htco_worker
  using (organization_id = (select private.worker_organization_id()))
  with check (organization_id = (select private.worker_organization_id()));

create table private.idempotency_keys (
  id uuid primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  key text not null constraint idempotency_keys_key_check check (
    char_length(key) between 1 and 255 and key ~ '^[\x21-\x7e]+$'
  ),
  method text not null constraint idempotency_keys_method_check check (
    method in ('POST', 'PUT', 'PATCH', 'DELETE')
  ),
  path text not null constraint idempotency_keys_path_check check (char_length(path) <= 2048),
  request_hash bytea not null
    constraint idempotency_keys_request_hash_check check (octet_length(request_hash) = 32),
  state text not null constraint idempotency_keys_state_check check (
    state in ('in_progress', 'completed')
  ),
  response_status integer constraint idempotency_keys_response_status_check check (
    response_status between 100 and 499
  ),
  response_body jsonb constraint idempotency_keys_response_body_check check (
    pg_column_size(response_body) <= 65536
  ),
  created_at timestamptz not null,
  expires_at timestamptz not null,
  constraint idempotency_keys_user_id_key_key unique (user_id, key),
  constraint idempotency_keys_lifetime_check check (
    expires_at > created_at and expires_at <= created_at + interval '24 hours'
  ),
  constraint idempotency_keys_completed_check check (
    (state = 'completed') = (response_status is not null)
  )
);
create index idempotency_keys_expires_at_idx on private.idempotency_keys (expires_at);

alter table private.idempotency_keys enable row level security;
revoke all on private.idempotency_keys from public, anon, authenticated, service_role;
