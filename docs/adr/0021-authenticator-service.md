# ADR-0021: An internal Authenticator service: PyOTP on the existing vault

- **Status:** Proposed (owner's architecture upgrade brief, 2026-10-23); accepted at the start of [Phase 8B](../plan/phases/phase-08b-authenticator.md)
- **Date:** 2026-10-23
- **Deciders:** Owner, engineering
- **Related:** [Token vault](../features/etsy-integration/token-vault.md) (AES-256-GCM envelope encryption), [ADR-0020](0020-per-shop-browser-environment.md), [ADR-0019](0019-unified-execution-engine-and-action-gateway.md), [THREAT_MODEL](../architecture/THREAT_MODEL.md)

> **Scope note (2026-10-26, #113).** This ADR is about the shops' accounts (Etsy, partners), whose TOTP secrets the browser worker needs. It does not cover HTCO's own sign-in: users' second factors (the authenticator app and passkeys) live in Supabase Auth ([passkeys](../features/identity-access/passkeys.md)).

## Context

A shop's Browser Environment needs to sign in to accounts protected by time-based one-time passwords (TOTP, RFC 6238): the shop's Etsy account and, later, POD partners, suppliers and email. The owner also wants to see a current code in the dashboard, for example when he signs in himself. Today the codes live on someone's phone, which blocks unattended sessions and keeps the shops dependent on one person's device.

## Decision drivers

- **One crypto scheme.** The token vault's AES-256-GCM envelope encryption, its key ring and its rotation are already built and tested. A second scheme would double the attack surface.
- **No model ever sees a secret or a code.**
- **Least privilege and a full audit** of every code generated, shown or used.
- **Free and open source.**

## Options considered

1. **A hosted secrets manager** (1Password Connect, Bitwarden Secrets Manager, HashiCorp Vault). Mature, but paid or extra infrastructure. It still needs our own RBAC and audit around it, and it would add a second crypto path.
2. **PyOTP inside the platform, with secrets sealed by the existing vault.** Adopted.
3. **Keep TOTP on the owner's phone** and hand every login to a person. Safe, but every session expiry becomes a human task. It stays the fallback.

## Decision

1. **The service.**
   - `htco_application.authenticator` implements the contracts in [execution-engine.md §6](../architecture/execution-engine.md#6-secret-broker-and-authenticator-contracts).
   - [PyOTP](https://github.com/pyauth/pyotp) (MIT) generates codes, server-side only.
   - Secrets are sealed by the existing `EnvelopeSealer` with associated data binding them to the organization, shop, account and purpose. The key ring and rotation are the token vault's; no new keys are introduced.
2. **Enrolment.**
   - The account owner pastes the `otpauth://` secret, or uploads the QR image. The QR is decoded server-side and the image is discarded at once, never stored.
   - Enrolment needs the account owner's explicit consent, recorded with text version, user and time, plus step-up re-authentication.
   - The first code is confirmed before the secret is saved.
   - A shop may have several TOTP accounts (`totp_accounts`: Etsy, a partner, email), each labelled. Accounts can be rotated (a new secret replaces the old one only after its first code is confirmed), replaced or removed. Removal destroys the ciphertext and is audited.
3. **Use.**
   - **Dashboard view.** An authorized user sees the current code and a countdown to the next period. Permission is by organization, shop and role, plus step-up. Each view is audited. The code is fetched per period, never cached in the browser and never logged.
   - **Browser worker.** The Secret Broker issues a one-time **injection grant** bound to one execution, one environment and one form field. The worker redeems it, receives the code over the internal channel and types it straight into the page. The calling actor (an automation or an employee) gets only `injected: true`.
   - **No tool, no prompt.** There is no tool that returns a code or a secret, and the registry has none. Model inputs are scanned for the secret's and code's patterns, and a match blocks the call.
4. **Controls.**
   - **Rate limits:** per account, at most 1 dashboard view per 30-second period per user and 10 per hour, and at most 3 injections per 10 minutes per environment.
   - **Replay protection:** a code is issued for injection at most once per period. Its hash is kept until the period ends; a second request in the same period waits for the next one.
   - **RBAC:** Owner and Admin enrol, rotate and remove. Viewing a code is a separate capability (`view_totp_code`) granted per shop. Automations and employees may *request an injection* for their execution only.
   - **Audit:** every enrolment, rotation, removal, view and injection, with who, which account, which execution and the outcome. Never the code.
5. **What TOTP never replaces.** WebAuthn and passkeys, CAPTCHA, email or SMS verification links, and any "confirm it's you" step go to a person (the Human channel). The authenticator handles TOTP only.

## Consequences

- Positive:
  - unattended sessions can sign in again without a phone;
  - one vault, one audit;
  - the LLM boundary is structural: no tool exists, rather than relying on a rule.
- Negative:
  - HTCO now holds second factors. A full compromise of the vault keys together with the database would expose them. Compensating controls: the keys live outside the database (as today), the code views are audited, and stored factors are listed on the shop's security page.
- Follow-ups:
  - Phase 8B builds it;
  - THREAT_MODEL gains the authenticator assets and threats;
  - a restore drill covers `totp_accounts`.

## Validation

Phase 8B tests:

- the sealed secret never appears in SQL output, logs or responses;
- a replayed injection in the same period is refused;
- a user without `view_totp_code` gets 404;
- a model call whose input contains a code is blocked;
- rotating the vault key re-wraps TOTP secrets as it does tokens.

## Validation log

- 2026-10-23 — Proposed with the plan update.
