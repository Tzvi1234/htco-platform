-- Etsy connect (slice 4.3, ADR-0008): pending OAuth authorizations, one shop per Etsy shop.
--
-- `POST /v1/shops/etsy/connect` keeps a pending authorization here for ten minutes: the SHA-256
-- of the random `state` (the state itself is never stored) and the PKCE code verifier, sealed by
-- the API (AES-256-GCM bound to the state hash) because the token exchange needs it back. It is
-- bound to the organization, the user and the web session that started it; ending (deleting)
-- the session deletes it. The callback deletes the row as it reads it, so a state works once.
-- Like the web sessions, the table lives in `private` with RLS on, no policy and no grant: only
-- the API's login role uses it.

create table private.oauth_authorizations (
  id uuid primary key,
  state_hash bytea not null constraint oauth_authorizations_state_hash_key unique
    constraint oauth_authorizations_state_hash_check check (octet_length(state_hash) = 32),
  provider text not null
    constraint oauth_authorizations_provider_check check (provider in ('etsy')),
  organization_id uuid not null references app.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  web_session_id uuid not null references private.web_sessions (id) on delete cascade,
  -- Sealed values start with the envelope's version byte (1): a plaintext verifier is refused.
  code_verifier bytea not null
    constraint oauth_authorizations_code_verifier_check check (get_byte(code_verifier, 0) = 1),
  scopes text[] not null,
  redirect_uri text not null
    constraint oauth_authorizations_redirect_uri_check check (char_length(redirect_uri) <= 2048),
  created_at timestamptz not null,
  expires_at timestamptz not null,
  constraint oauth_authorizations_lifetime_check check (
    expires_at > created_at and expires_at <= created_at + interval '10 minutes'
  )
);
create index oauth_authorizations_user_id_idx on private.oauth_authorizations (user_id);
create index oauth_authorizations_web_session_id_idx
  on private.oauth_authorizations (web_session_id);

alter table private.oauth_authorizations enable row level security;
revoke all on private.oauth_authorizations from public, anon, authenticated, service_role;

-- One integration per Etsy shop and organization (AC-4.3.2): reconnecting updates that row.
create unique index shop_integrations_organization_id_provider_etsy_shop_id_key
  on app.shop_integrations (organization_id, provider, etsy_shop_id);
-- The token provider finds a shop's integration by the Etsy shop ID.
create index shop_integrations_etsy_shop_id_idx
  on app.shop_integrations (etsy_shop_id) where status <> 'disconnected';
