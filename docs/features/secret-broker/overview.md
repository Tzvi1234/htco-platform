# Secret Broker and Authenticator — Overview

| | |
|---|---|
| **Slug** | `secret-broker` |
| **Phase / slices** | Phase 5 — slice 5.17 (contracts, in-memory implementations, the PyOTP adapter, the registry rule); tables, enrolment UI and the code view in Phase 8B |
| **Decision** | [ADR-0021](../../adr/0021-authenticator-service.md), [ADR-0020](../../adr/0020-per-shop-browser-environment.md); design [execution-engine.md §6](../../architecture/execution-engine.md#6-secret-broker-and-authenticator-contracts) |
| **Status** | Built and tested in CI. Nothing is deployed: no process stores a credential or a TOTP secret before Phase 8B. |
| **Last updated** | 2026-10-24 |

## What and why

A shop's browser environment will need the shop's Etsy password and TOTP codes to sign in again without a person. These are second factors and credentials, so they are held to the strictest rules: one vault, no model ever sees them, every access is audited, and a code is used once.

## Acceptance criteria

| ID | Criterion | Status |
|---|---|---|
| AC-5.17.1 | `SecretValue` never appears in `repr`, logs, JSON or exception messages (property tests over the fakes and log capture). | Pass |
| AC-5.17.2 | A grant is single use, bound to one execution and environment, and expires within 60 s (fake broker tests). | Pass |
| AC-5.17.3 | The registry check fails if any tool's output contract can carry a secret or a one-time code. | Pass |

## Scope

- **In:**
  - `htco_domain.secrets`: `SecretValue`, `SecretRef`, `InjectionGrant`, `DisplayedCode` and `period_window`. `ConsentRecord` is in `htco_domain.browser` (5.16).
  - `htco_application.secrets`:
    - the `SecretBroker`, `Authenticator`, `SecretVault`, `TotpCodes` and `SecretAudit` ports;
    - `MemorySecretBroker`, `MemoryAuthenticator` and `MemorySecretAudit`;
    - `SecretScanner` and `SecretLogFilter`;
    - `secret_carrying_fields` and `secret_tool_names`.
  - `htco_integrations.totp.PyOtpCodes`;
  - the action catalog's refusal of a secret-carrying output.
- **Out (8B):**
  - the tables (`shop_credentials`, `totp_accounts`, `totp_events`, `secret_grants`);
  - QR decoding, the enrolment and code-view UI, the `view_totp_code` capability;
  - wiring the scanner into the model gateway.

## Known limits

- The scanner matches what was released through the broker and the authenticator, by SHA-256 fingerprint of whole tokens. A secret split across words, or one that never passed through them, is not caught. The structural rule still holds: no tool returns a secret.
- Rate limits, replay state and the stores are in memory until Phase 8B.
