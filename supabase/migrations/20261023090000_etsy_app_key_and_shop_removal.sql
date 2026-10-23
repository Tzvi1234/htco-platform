-- Replacing the Etsy app and removing a shop.
--
-- Tokens are issued per Etsy app. `app_key_fingerprint` records which app a connection's tokens
-- belong to: the first 16 hex characters of the keystring's SHA-256, never the key itself (the
-- keystring is a public client ID anyway). When the configured key changes, the API treats every
-- connected row with another fingerprint as `needs_reauth` without calling Etsy and stores that
-- status at startup. Rows connected before this column existed stay null until their first
-- successful call or refresh records the current key.
alter table app.shop_integrations
  add column app_key_fingerprint text
  constraint shop_integrations_app_key_fingerprint_check
    check (app_key_fingerprint is null or app_key_fingerprint ~ '^[0-9a-f]{16}$');

-- The shop card reads it as the caller, to show such a connection as needing reconnection.
grant select (app_key_fingerprint) on app.shop_integrations to authenticated;

-- The startup check looks for connected rows of another key.
create index shop_integrations_connected_app_key_idx
  on app.shop_integrations (app_key_fingerprint) where status = 'connected';

-- Removing a shop (Owner, `aal2`, step-up) runs as the API's login role in one transaction: it
-- deletes the integration with its tokens, the probe results, the stored order history (orders,
-- items, refunds, buyers, sync state), the members' shop grants and finally the shop. Every
-- table that names a shop already references `app.shops` with `on delete cascade`, so nothing
-- of the shop can outlive it; no grant changes: no client role may delete a shop.
