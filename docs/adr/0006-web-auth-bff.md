# ADR-0006: Web authentication through a backend-for-frontend

- **Status:** Accepted
- **Date:** 2026-10-14
- **Deciders:** Owner, engineering

## Context

The web dashboard shows orders, buyer data and money, and lets the owner approve actions that spend money or publish listings. Supabase's JavaScript client can keep sessions in the browser, but then any cross-site scripting bug can steal long-lived refresh tokens. The IETF best current practice for browser-based OAuth apps ([RFC 10017](https://www.rfc-editor.org/rfc/rfc10017), August 2026) strongly recommends a backend-for-frontend (BFF) for business and sensitive applications.

## Decision drivers

- No tokens readable by page scripts.
- Same rules and audit for web and native clients.
- Simple CSRF protection.

## Options considered

1. **Tokens in the browser** (Supabase JS with PKCE, storage in memory or local storage). Simple, but tokens are exposed to XSS.
2. **BFF in the FastAPI API.** The API runs the Supabase PKCE exchange server-side and gives the browser an opaque session cookie.
3. **A separate BFF service.** Same security as option 2 with one more deployable.

## Decision

Option 2:

- The web app is served from `app.ht-co.app`, and the API is routed at `app.ht-co.app/api` (same origin), so no CORS is needed for the web.
- The API stores Supabase tokens server-side (encrypted) and sets a session cookie named with the `__Host-` prefix, `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, no `Domain`.
- Every unsafe request must carry a custom header (for example `X-HTCO-CSRF`) and pass `Origin` and `Sec-Fetch-Site` checks.
- Native apps (Windows, Android) use bearer tokens from PKCE through the system browser; CSRF checks apply only to cookie-authenticated requests.
- MFA enrollment and verification run through the same BFF endpoints.

## Consequences

- Positive: no token theft through XSS; one authorization path; session revocation is immediate on the server.
- Negative: the API holds session state (a small sessions table) and implements the auth flow endpoints.
- Follow-ups: confirm in Phase 2 whether the Python Supabase client supports the server-side PKCE exchange, or call the Auth REST endpoints directly; any `*.ht-co.app` subdomain takeover would weaken same-site assumptions, so unused DNS records are removed promptly.

## Validation

Phase 3 security tests: cookie attributes, CSRF rejection without the header or with a cross-site origin, session revocation, no token in any browser storage (checked by an end-to-end test).

Because the session cookie is `SameSite=Strict`, it is not sent on redirects arriving from other sites. Third-party OAuth callbacks (Etsy) therefore land on a web-app route that posts the result to the API with a same-site request (see [ADR-0008](0008-etsy-integration.md)).

## Validation log

No entries yet.
