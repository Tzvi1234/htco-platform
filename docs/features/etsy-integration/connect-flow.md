# Etsy integration — Connect flow and token refresh (slices 4.3 and 4.4)

The owner connects an Etsy shop from the web app with OAuth 2.0 and PKCE; the API stores the tokens in the [token vault](token-vault.md), creates or updates the shop, and refreshes the access token on demand ([phase 4](../../plan/phases/phase-04-etsy-connection.md), [ADR-0008](../../adr/0008-etsy-integration.md) decisions 9 and 10). This page covers the platform side; the web pages (the connect button, `/integrations/etsy/callback`, the shop card) follow in `htco-clients`.

| AC | Status |
|---|---|
| AC-4.3.1 A callback with a missing, reused, expired or foreign `state` is rejected and audited | Done |
| AC-4.3.2 Connecting the same Etsy shop twice updates one shop record | Done |
| AC-4.3.3 Connecting grants shop access to no user other than the connecting owner | Done |
| AC-4.3.4 Reconnecting with more scopes updates the scopes on the same record | Done |
| AC-4.4.1 Ten concurrent requests on an expired token cause exactly one refresh call | Done |
| AC-4.4.2 A revoked refresh token leads to `needs_reauth`, audited, with no retry loop | Done at the platform; the shop card and overview banner come with slice 4.6 |

## Design

```mermaid
sequenceDiagram
  participant B as Browser (dash.ht-co.app)
  participant A as API
  participant DB as Postgres
  participant E as Etsy
  B->>A: POST /v1/shops/etsy/connect (cookie, X-HTCO-CSRF; Owner, aal2, step-up)
  A->>DB: private.oauth_authorizations: sha256(state), sealed verifier, org, user, web session, +10 min
  A-->>B: {authorization_url} (no-store)
  B->>E: www.etsy.com/oauth/connect?…state…code_challenge (S256)
  E-->>B: 302 dash.ht-co.app/api/v1/integrations/etsy/callback?code&state
  B->>A: GET /v1/integrations/etsy/callback?code&state (cross-site: no session cookie)
  A-->>B: 303 /integrations/etsy/callback (clean URL), sealed HttpOnly cookie __Host-htco_etsy_callback (10 min), no-referrer
  B->>A: POST /v1/integrations/etsy/callback, no body (same-site: session and callback cookies sent)
  A->>DB: delete … where state_hash = sha256(state) returning … (single use)
  A->>A: same user and web session? not expired? still Owner with aal2?
  A->>E: POST /v3/public/oauth/token (authorization_code + code_verifier)
  A->>E: GET /v3/application/users/{user_id}/shops
  A->>DB: one transaction: shop, owner grant, sealed tokens, scopes, audit
  A-->>B: {shop, integration, reconnected} (no-store)
```

### Endpoints

| Endpoint | Who | Request | Answer |
|---|---|---|---|
| `POST /v1/shops/etsy/connect` | Owner, `aal2`, step-up in the last 10 minutes (`connect_shop` in the permission matrix), web session with CSRF header and allowed origin | No body | 200 `{"authorization_url": "https://www.etsy.com/oauth/connect?…"}`, `Cache-Control: no-store` |
| `GET /v1/integrations/etsy/callback` (Codex security review, owner gate v4) | Etsy's redirect; no session needed | `code` (≤2048), `state` (≤512), `error` in Etsy's query | 303 to the web app's clean `/integrations/etsy/callback` (`?outcome=cancelled`, `failed` or `incomplete` when there is no code), with the code and state sealed in the `__Host-htco_etsy_callback` cookie (`HttpOnly; Secure; SameSite=Strict`, 10 minutes); `no-store`, `Referrer-Policy: no-referrer`. Nothing is redeemed yet. Generic: a first connection, a reconnection and a re-consent for more scopes all finish here |
| `POST /v1/integrations/etsy/callback` | The same web session (CSRF); Owner with `aal2` (the step-up was proven at the start) | No body: the code and state come from the callback cookie, removed whatever the outcome (no cookie is rejected and audited as a missing state) | 200 `{"shop": ShopView, "integration": IntegrationView, "reconnected": bool}`, `no-store` |
| `GET /v1/shop-integrations?organization_id=` | Owner and Admin (organization-wide roles); no MFA needed to look | — | 200 `{"items": [IntegrationView]}` |
| `POST /v1/shop-integrations/{shop_id}/capability-checks` (slice 4.5) | Owner and Admin; CSRF for web sessions | No body | 200 `{"shop_id", "capabilities": [CapabilityView]}`; 404, 409 `shop-not-connected`, 429 `capability-check-too-soon` (`Retry-After`), 503 `etsy-unavailable` |
| `POST /v1/shop-integrations/{shop_id}/disconnect` (slice 4.7) | Owner, `aal2`, step-up in the last 10 minutes (`connect_shop`); CSRF | No body | 200 `IntegrationView` (status `disconnected`, `purge_after`); 403 `forbidden`, `mfa-required`, `step-up-required`, `csrf-failed`; 404 |

`IntegrationView`: `shop_id`, `provider` (`etsy`), `status` (`connected`, `needs_reauth`, `disconnected`), `etsy_shop_id`, `scopes` (sorted), `token_health` (`ok`, `expiring` within 10 days of the refresh token's end, `needs_reauth`, `disconnected`), `access_expires_at`, `refresh_expires_at`, `failure_count`, `last_successful_call_at`, `connected_at`, `updated_at`, `disconnected_at`, `purge_after` and `capabilities` (`CapabilityView`: `capability`, `state` `available`/`unavailable`/`unknown`, `detail` code, `checked_at`; slices 4.5 and 4.7, see [operations](operations.md)). It never carries a token. The callback's answer includes the capabilities probed right after connecting.

Problems (all `application/problem+json`, `https://ht-co.app/problems/<slug>`):

| Slug | Status | When |
|---|---|---|
| `etsy-unavailable` | 503 | The connect flow is off: Etsy credentials, `HTCO_ETSY_REDIRECT_URI`, the vault key or web sign-in missing. Title "Etsy connection unavailable" |
| `etsy-state-rejected` | 400 | The state is missing, unknown or already used, expired, or from another user or web session |
| `etsy-authorization-refused` | 400 | Etsy answered `invalid_grant` to the code exchange |
| `etsy-unreachable` | 502 | Etsy's token endpoint or shop lookup failed otherwise; nothing was stored |
| `etsy-no-shop` | 409 | The Etsy account that approved has no shop |
| `etsy-shop-connected-elsewhere` | 409 | That Etsy shop is connected (not disconnected) in another organization |
| `web-session-required` | 403 | A bearer (non-cookie) request to the callback |
| `forbidden`, `mfa-required`, `step-up-required`, `csrf-failed` | 403 | As everywhere ([policy guard](../../../apps/api/src/htco_api/policy_guard.py), [web auth](../../../apps/api/src/htco_api/web_auth.py)) |

The web session ID reaches the routes through `request.state.web_session_id`, set by `require_auth` when the cookie authenticated the request. Owners with several organizations are refused (`forbidden`) by `require_capability` until a route names the organization; today every owner has one.

### Layers

| Layer | Module | Role |
|---|---|---|
| Domain | `htco_domain.integrations` | `TokenHealth`, `token_health()`, `REFRESH_EXPIRY_WARNING` (10 days) |
| Application | `htco_application.etsy_connect` | `start_connection`, `complete_connection`; ports `EtsyAuthorizationServer`, `EtsyShopDirectory`, `PendingAuthorizations`, `ShopConnector`, `AuditLog`; `TokenGrant`, `PendingAuthorization`, `ShopConnectionRequest` |
| Application | `htco_application.etsy_tokens` | `VaultAccessTokenProvider` (the adapter's `AccessTokenProvider`), `RefreshLock` |
| Application | `htco_application.shop_integrations` | `IntegrationSummary`, `IntegrationReader`, `list_integrations`; vault port gains `find_by_marketplace_shop` and an audited `mark_needs_reauth` |
| Adapters | `htco_integrations.etsy_oauth.EtsyOAuthClient`; `EtsyAdapter.shop_of_user` | Etsy's OAuth endpoints; getShopByOwnerUserId with the new token |
| Adapters | `htco_persistence.etsy_connect` (`PostgresPendingAuthorizations`, `PostgresShopConnector`), `htco_persistence.shop_integrations` (`PostgresRefreshLock`, `PostgresIntegrationReader`), `htco_persistence.audit.PostgresAuditLog` | Storage |
| API | `htco_api.etsy`, wiring in `htco_api.main._etsy_wiring` | Routes, problems, composition |

### Data

Migration `supabase/migrations/20261015180000_etsy_connect.sql`:

- `private.oauth_authorizations`: `state_hash` (SHA-256, unique, 32 bytes), `provider`, `organization_id`, `user_id`, `web_session_id` (foreign key to `private.web_sessions`, cascade), `code_verifier` (sealed with the web session key, AES-256-GCM bound to the state hash; the version-byte check refuses plaintext), `scopes`, `redirect_uri`, `created_at`, `expires_at` (at most 10 minutes later, checked). RLS on, no policy, no grant to any client role. `take` deletes the row as it reads it, so concurrent callbacks with one state have exactly one winner; saving a new authorization deletes the user's expired ones.
- `app.shop_integrations`: a partial index on `etsy_shop_id` (rows not disconnected) for the token provider and the connect check. One integration per Etsy shop and organization is kept by the connect transaction (advisory lock per Etsy shop, then update of the existing row), not by a unique index: such an index would fire before the composite foreign key that keeps a row inside its organization (pgTAP `05_shop_integrations` test 18 expects that foreign-key error). New column `last_refresh_failed_at` (when the last refresh failed; not granted to client roles; cleared by a successful refresh or a reconnect).

### Connecting (one transaction, login role)

`PostgresShopConnector.connect` takes `pg_advisory_xact_lock` on the Etsy shop ID, refuses a shop connected in another organization, finds this organization's integration for that Etsy shop (`for update`), inserts or updates the `app.shops` row (name from Etsy, status `active`), inserts one `app.shop_access` row for the connecting Owner's membership (nobody else is granted, AC-4.3.3; the Owner already reaches every shop through the role, so the grant only keeps access if the role changes later), upserts the sealed tokens and the granted scopes through the vault (`store_in`), and appends `shop.connected` or `shop.reconnected` (before: previous scopes and status; after: Etsy shop ID, name, scopes) to `audit.audit_log`, with the request ID as correlation ID.

Scopes recorded are those Etsy names in the token answer's `scope`; when Etsy names none, the requested ones (`shops_r transactions_r listings_r`).

### Refresh (slice 4.4)

`VaultAccessTokenProvider.access_token(etsy_shop_id)`:

1. Find the integration by Etsy shop ID (not disconnected). Missing or not `connected`: `MarketplaceAuthError` (reconnect).
2. More than 5 minutes left: return the stored access token.
3. Otherwise take the in-process lock for the shop, then `PostgresRefreshLock` (a session-level `pg_advisory_lock` on one pooled connection, released explicitly or when the connection dies), read the row again (another caller may have refreshed it), and call Etsy's `refresh_token` grant once.
4. Success: store the new tokens (`update_tokens`, failures reset). A new refresh token gets a new 90-day expiry; the same one keeps its expiry. A row disconnected meanwhile is not restored. Every write after the OAuth call (tokens, failure count, `needs_reauth`) names the connection generation the refresh started under (slice 4.7), so a disconnect and reconnect during the call leave the new connection untouched and the caller gets `MarketplaceAuthError`.
5. `invalid_grant`: `needs_reauth` at once, audited `shop.integration_needs_reauth` (`reason: invalid_grant`) in the same transaction, `MarketplaceAuthError`. Further calls fail fast without contacting Etsy.
6. Any other failure: `failure_count + 1` and `last_refresh_failed_at` set; at 3, `needs_reauth` audited (`reason: refresh_failed`). Below 3, the old token is returned while it still works, else `MarketplaceUnavailableError`. No loop: each call makes at most one refresh attempt.
7. For `REFRESH_RETRY_INTERVAL` (1 minute) after a failed attempt nobody tries again: callers that queued behind it on the lock, in any process, and calls during the same outage share its result (the old token while valid, else `MarketplaceUnavailableError`) instead of each spending one of the three tries (Codex review of this pull request). Three failures therefore span at least two minutes of outage.

The shop card reads `refresh_expires_at` and `token_health` from `GET /v1/shop-integrations`; `expiring` starts 10 days before the end. Scheduled checks and email arrive with Phases 5 and 6.

## Security

Threats beyond the [vault's](token-vault.md#security) and the [adapter's](security.md):

| Threat | Control | Test |
|---|---|---|
| Login CSRF on the callback: an attacker's code and state posted from the victim's browser (connecting the attacker's shop, or ours to them) | State bound to user and web session; foreign state rejected and audited; callback needs CSRF header and allowed origin | `test_complete__state_of_another_session__*`, `test_callback__state_from_another_browser_session__400_rejected`, `test_callback__without_csrf__403_and_the_state_stays_usable` |
| Replay of a state or code | State row deleted on first read (single use, concurrent takes have one winner); Etsy codes are single-use | `test_complete__reused_state__*`, `test_pending__concurrent_takes__exactly_one_wins` |
| Authorization-code interception | PKCE S256 with a 64-byte random verifier; the verifier never leaves the server and is sealed at rest | `test_start__authorization_url_has_pkce_s256_*`, `test_pending__taken_once_and_the_verifier_is_sealed_at_rest`, pgTAP `06_etsy_connect` |
| State guessing | 256-bit `secrets.token_urlsafe` state, stored only as SHA-256 | `test_start__state_has_at_least_128_bits_and_is_stored_only_as_a_hash` |
| A non-owner, a demoted owner or a session without MFA connects | `connect_shop` (Owner, `aal2`, step-up) at the start; Owner and `aal2` re-checked at the callback | `test_connect__*_403_*`, `test_callback__caller_demoted_since_the_start__403_forbidden`, `test_complete__caller_no_longer_owner_with_mfa__*` |
| Cross-tenant: one Etsy shop's tokens in two organizations, or a token used for the wrong tenant | Connecting a shop connected elsewhere is refused under an advisory lock; the provider refuses to guess between two rows | `test_connect__etsy_shop_connected_in_another_organization__refused`, `test_callback__shop_connected_in_another_organization__409` |
| Over-broad access after connecting | Only the connecting Owner gets a grant | `test_connect__creates_the_shop_integration_owner_grant_and_audit_in_one_go` |
| Tokens, codes or verifiers in responses, logs, errors or audit | Views carry no token; `repr=False` on every token field; errors and logs name only the grant type and status; audit records carry scopes and reasons | `test_no_endpoint_response__ever_contains_a_token_code_or_verifier`, `test_token_requests__never_log_or_raise_with_a_token_code_or_secret`, `test_access_token__errors_never_carry_a_token`, `test_main__everything_configured__etsy_connect_on_and_no_secret_logged` |
| Refresh storms or loops locking the shop out | One refresh under per-shop locks; no retries; a failed attempt shared for a minute; `needs_reauth` after `invalid_grant` or three spaced failures | `test_refresh__ten_concurrent_requests_in_two_processes__one_refresh_call`, `test_access_token__callers_waiting_during_one_failed_refresh__share_its_result`, `test_access_token__*` |
| Open redirect through the redirect URI | Fixed from configuration (`HTCO_ETSY_REDIRECT_URI`, HTTPS, no query or fragment when deployed), never from the request | `test_etsy_redirect_uri__*`, `test_spec__etsy_redirect_uri_is_the_apis_callback` |
| Authorization code and state in the web app's URL, static-host logs or same-origin referrers | The redirect URI is the API's `GET` callback, which seals them into an `HttpOnly` cookie and redirects to a clean page with `no-referrer`; the web app's document policy is `no-referrer` too | `test_redirect__seals_code_and_state_in_an_httponly_cookie_and_cleans_the_url`, `test_callback__the_cookie_is_spent_after_one_try`, `test_callback__without_the_cookie__400_rejected_and_audited` |

ASVS 5.0, chapter V10 (OAuth and OIDC), client side (exact requirement numbers to be confirmed in the phase security review, as for slice 4.2):

| Requirement | How |
|---|---|
| 10.2.1 CSRF protection of the code flow: `state` (or PKCE) checked against the user agent's session | Random state bound to the web session, single use, plus PKCE |
| 10.2.2 / 10.4.6 PKCE with S256 for the code grant | `code_challenge_method=S256`, verifier 86 base64url characters |
| 10.2.3 Least privilege: only the scopes needed | `shops_r transactions_r listings_r`; more only by re-consent (ADR-0008) |
| 10.4.1 Exact redirect URI | One configured value, sent unchanged in both requests, registered exactly at Etsy |
| 10.4.x Codes used once and short-lived | Etsy's single-use codes; the pending authorization expires in 10 minutes and is deleted on use |
| 10.3.x / 13.3.1 Tokens and client secret kept server-side | Tokens only in the vault (sealed); keystring and shared secret are secrets from the environment; the browser only ever sees the authorization URL |
| 16.2.5 No sensitive data in logs | Log-capture tests above |

Rule of Two: no agent is involved; not applicable.

## Tests

| AC | Tests |
|---|---|
| AC-4.3.1 | `tests/application/test_etsy_connect.py::test_complete__missing_state__*`, `::test_complete__unknown_state__*`, `::test_complete__reused_state__*`, `::test_complete__expired_state__*`, `::test_complete__state_of_another_session__*`, `::test_complete__state_of_another_user__*`; `tests/api/test_etsy_connect_routes.py::test_callback__missing_state__*`, `::test_callback__reused_state__*`, `::test_callback__expired_state__*`, `::test_callback__state_from_another_browser_session__*`; `tests/integration/test_etsy_connection.py::test_pending__*`; pgTAP `supabase/tests/database/06_etsy_connect.test.sql` |
| AC-4.3.2 | `test_complete__connecting_the_same_shop_twice__one_shop`, `test_callback__same_shop_twice__one_shop_reconnected`, `tests/integration/test_etsy_connection.py::test_connect__same_etsy_shop_twice__updates_one_shop` |
| AC-4.3.3 | `test_complete__exchanges_the_code_with_the_verifier_and_connects_the_shop`, `test_callback__connects_the_shop_and_answers_the_shop_view`, `tests/integration/test_etsy_connection.py::test_connect__creates_the_shop_integration_owner_grant_and_audit_in_one_go` (one grant; the operations member sees no shop through RLS) |
| AC-4.3.4 | `test_complete__records_the_scopes_etsy_granted_when_it_names_them`, `test_callback__reconnect_with_more_scopes__scopes_updated`, `tests/integration/test_etsy_connection.py::test_connect__reconnect_with_more_scopes__same_record_new_scopes` |
| AC-4.4.1 | `tests/application/test_etsy_tokens.py::test_access_token__ten_concurrent_requests_on_an_expired_token__one_refresh`, `tests/integration/test_etsy_connection.py::test_refresh__ten_concurrent_requests_in_two_processes__one_refresh_call` (two pools, as two API processes) |
| AC-4.4.2 | `test_access_token__refresh_token_revoked__needs_reauth_audited_without_retry`, `test_access_token__three_failed_refreshes__needs_reauth_after_the_third`, `test_access_token__callers_waiting_during_one_failed_refresh__share_its_result`, `tests/integration/test_etsy_connection.py::test_refresh_failure__records_when_and_a_new_token_clears_it`, `tests/integration/test_etsy_connection.py::test_mark_needs_reauth__with_audit__once_and_atomically`; health shown by `test_integrations__needs_reauth_is_shown` |
| AC-4.2.3 (extended) | `test_no_endpoint_response__ever_contains_a_token_code_or_verifier` |

Also: `tests/integrations/test_etsy_oauth.py` (authorization URL, form posts, every token failure mapped once without retry, `shop_of_user`), `tests/domain/test_integrations.py` (token health boundaries), `tests/api/test_etsy_wiring.py` (on only when every piece is set; blank secrets keep the API booting), `tests/api/test_settings.py::test_etsy_redirect_uri__*`, `tests/infra/test_app_spec.py` (Etsy secrets and redirect URI in the app spec and deploy workflow).

Database tests run in CI's `database` job (`just test-db`, `just db-test`).

### Verification — 4.3 and 4.4 — 2026-10-15

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-4.3.1 to AC-4.3.4 | Above | API flow driven end to end through `httpx.ASGITransport` with a real cookie session, MFA and step-up, and fakes for Etsy and storage; storage against Postgres in CI | Pass |
| AC-4.4.1, AC-4.4.2 | Above | Two database pools racing ten requests (CI database job) | Pass in CI |

**Adversarial checks run:** callbacks without CSRF header, from another origin, with a bearer token, from a second browser session, after expiry, reused, with a demoted caller; Etsy refusing the code, timing out, answering garbage; a shop already connected in another organization; responses scanned for every token, code and verifier.
**Observed:** `api.starting` shows `etsy_connect` on or off and never a secret (log-capture test).
**Deployed check:** not yet. Needs the owner: `HTCO_TOKEN_VAULT_KEY` and the redirect URI registered in the Etsy app, then a connection of the pilot shop with the owner present (phase exit gate).
**Open issues:** the web pages (connect button, callback route, shop card) are the next pull request in `htco-clients`.

## Operations

| Setting | Value |
|---|---|
| `HTCO_ETSY_KEYSTRING`, `HTCO_ETSY_SHARED_SECRET` | Secrets; deployed from GitHub secrets `ETSY_KEYSTRING`, `ETSY_SHARED_SECRET` |
| `HTCO_ETSY_REDIRECT_URI` | `https://dash.ht-co.app/api/v1/integrations/etsy/callback` (in `infra/digitalocean/app.yaml`); must match the Etsy app's registered callback exactly (re-registered by the owner, [OWNER_REMINDERS](../../operations/OWNER_REMINDERS.md)) |
| `HTCO_TOKEN_VAULT_KEY` | Secret (see [token vault](token-vault.md#configuration)); without it connecting stays off |
| `HTCO_SESSION_KEY` | Also seals the pending PKCE verifiers; rotating it drops pending authorizations (owners click "Connect" again) |

The flow is on when `api.starting` shows `"etsy_connect": "on"`. Off: `POST /v1/shops/etsy/connect` answers 503 `etsy-unavailable` (after the sign-in checks).

Runbook:

- **`etsy-state-rejected` for every attempt:** check the audit log's `shop.connect_rejected` reasons. `foreign`: the callback page ran in another browser or the user signed in again in between. `expired`: more than 10 minutes on Etsy's page. `unknown`: reused link, or `HTCO_SESSION_KEY` changed.
- **`etsy-authorization-refused`:** the redirect URI sent differs from the one registered at Etsy, or the code was used twice. Compare `HTCO_ETSY_REDIRECT_URI` with the Etsy app settings.
- **A shop shows `needs_reauth`:** the audit record says why (`invalid_grant`: the owner revoked access at Etsy or the refresh token expired; `refresh_failed`: three failed refreshes). The owner reconnects; reconnecting resets the status and failures.

Rollback: the code can be rolled back; the migration is additive (the new table and indexes stay unused).
