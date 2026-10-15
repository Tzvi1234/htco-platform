-- Shop integrations: the Etsy token vault (slice 4.2, ADR-0008).
--
-- One row per shop and provider. The API seals the OAuth tokens before they arrive here (envelope
-- encryption: a data key per row, AES-256-GCM, wrapped by a key-encryption key from the
-- environment whose ID is `key_id`), so no column ever holds a plaintext token and a database dump
-- alone exposes none. The sealed values are bound to the row's shop and provider, so moving them
-- to another row does not open them.
--
-- Only the API's login role writes and reads the sealed columns. An Owner or Admin may read the
-- other columns of their organization's rows (status, scopes, expiry: the shop card); the sealed
-- columns and the key ID are not granted to any client role, so even `select *` is refused.

create table app.shop_integrations (
  id uuid primary key,
  organization_id uuid not null,
  shop_id uuid not null,
  provider text not null constraint shop_integrations_provider_check check (provider in ('etsy')),
  etsy_shop_id bigint not null constraint shop_integrations_etsy_shop_id_check check (etsy_shop_id > 0),
  etsy_user_id bigint not null constraint shop_integrations_etsy_user_id_check check (etsy_user_id > 0),
  -- The envelope: key ID, wrapped data key, sealed tokens. Null once disconnected.
  key_id text constraint shop_integrations_key_id_check check (key_id ~ '^[A-Za-z0-9._-]{1,32}$'),
  wrapped_data_key bytea,
  access_token_ciphertext bytea,
  refresh_token_ciphertext bytea,
  scopes text[] not null default '{}',
  access_expires_at timestamptz,
  refresh_expires_at timestamptz,
  status text not null constraint shop_integrations_status_check check (
    status in ('connected', 'needs_reauth', 'disconnected')
  ),
  failure_count integer not null default 0
    constraint shop_integrations_failure_count_check check (failure_count >= 0),
  last_successful_call_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint shop_integrations_shop_fkey foreign key (shop_id, organization_id)
    references app.shops (id, organization_id) on delete cascade,
  constraint shop_integrations_shop_id_provider_key unique (shop_id, provider),
  -- Sealed values start with the envelope's version byte (1): a plaintext token is refused.
  constraint shop_integrations_sealed_check check (
    (wrapped_data_key is null or get_byte(wrapped_data_key, 0) = 1)
    and (access_token_ciphertext is null or get_byte(access_token_ciphertext, 0) = 1)
    and (refresh_token_ciphertext is null or get_byte(refresh_token_ciphertext, 0) = 1)
  ),
  -- Disconnected means the tokens are deleted; otherwise the whole envelope is present.
  constraint shop_integrations_tokens_check check (
    case when status = 'disconnected' then
      num_nonnulls(key_id, wrapped_data_key, access_token_ciphertext, refresh_token_ciphertext,
                   access_expires_at, refresh_expires_at) = 0
    else
      num_nulls(key_id, wrapped_data_key, access_token_ciphertext, refresh_token_ciphertext,
                access_expires_at, refresh_expires_at) = 0
    end
  )
);
create index shop_integrations_organization_id_idx on app.shop_integrations (organization_id);
-- Rotation looks for rows still wrapped by a previous key.
create index shop_integrations_key_id_idx on app.shop_integrations (key_id) where key_id is not null;

create trigger shop_integrations_set_updated_at before update on app.shop_integrations
  for each row execute function private.set_updated_at();

alter table app.shop_integrations enable row level security;
revoke all on app.shop_integrations from public, anon, authenticated, service_role;

grant select (
  id, organization_id, shop_id, provider, etsy_shop_id, etsy_user_id, scopes, access_expires_at,
  refresh_expires_at, status, failure_count, last_successful_call_at, created_at, updated_at
) on app.shop_integrations to authenticated;

create policy shop_integrations_select on app.shop_integrations for select to authenticated
  using (organization_id = any (array(select private.user_admin_org_ids())));
