-- Web sessions for the backend-for-frontend (slice 3.5, ADR-0006).
--
-- The browser holds an opaque cookie; this table keeps its SHA-256 hash and the user's Supabase
-- tokens, sealed by the API (AES-256-GCM bound to the session ID) before they arrive here. Only the
-- API's login role uses these tables: they live in `private`, have RLS on with no policy, and no
-- client role (`anon`, `authenticated`, `service_role`) holds any privilege on them.

create table private.web_sessions (
  id uuid primary key,
  token_hash bytea not null constraint web_sessions_token_hash_key unique,
  user_id uuid not null references auth.users (id) on delete cascade,
  access_token bytea not null,
  refresh_token bytea not null,
  access_expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  idle_expires_at timestamptz not null,
  absolute_expires_at timestamptz not null,
  reauthenticated_at timestamptz,
  revoked_at timestamptz,
  user_agent text constraint web_sessions_user_agent_check check (char_length(user_agent) <= 300)
);
create index web_sessions_user_id_idx on private.web_sessions (user_id) where revoked_at is null;

-- Sign-in attempts for throttling (AC-3.5.5): hashes only, never the email or the address.
create table private.sign_in_attempts (
  id bigint generated always as identity primary key,
  email_hash bytea not null,
  ip_hash bytea,
  succeeded boolean not null,
  attempted_at timestamptz not null default now()
);
create index sign_in_attempts_email_hash_attempted_at_idx
  on private.sign_in_attempts (email_hash, attempted_at desc);
create index sign_in_attempts_ip_hash_attempted_at_idx
  on private.sign_in_attempts (ip_hash, attempted_at desc);

alter table private.web_sessions enable row level security;
alter table private.sign_in_attempts enable row level security;
revoke all on private.web_sessions, private.sign_in_attempts
  from public, anon, authenticated, service_role;
