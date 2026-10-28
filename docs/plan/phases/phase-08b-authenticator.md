# Phase 8B — Authenticator

| | |
|---|---|
| **ID** | 8B (added 2026-10-23; numbering in [PLAN §2](../PLAN.md#2-phases-and-milestones)) |
| **Milestone** | — |
| **Repositories** | `htco-platform`, `htco-clients` (code view, enrolment screens) |
| **Size** | M |
| **Depends on** | [Phase 4](phase-04-etsy-connection.md) (token vault), [Phase 5](phase-05-orders-ingest.md) (Secret Broker and Authenticator contracts, slice 5.17), [Phase 7](phase-07-tasks-approvals.md) (step-up rules, audit and activity records) |
| **Parallel with** | Phase 8 and Phase 8A |
| **Decision** | [ADR-0021](../../adr/0021-authenticator-service.md) |
| **Status** | PLANNED |

## Goal

An internal Authenticator that the Browser Environment and the owner both use:

- TOTP accounts enrolled with consent, several per shop, rotated safely;
- codes generated only on the server;
- a code view with a countdown in the dashboard for authorized users;
- codes injected straight into a browser worker for its sign-in.

Secrets and codes never reach a model, a tool, a log or an audit row.

## Scope

**In:**

- `totp_accounts` and `totp_events`;
- enrolment by secret or QR (decoded server-side, the image discarded);
- rotation, replacement and removal;
- the Secret Broker's injection grants;
- the dashboard code view;
- RBAC (`manage_totp`, `view_totp_code`, `request_totp_injection`);
- rate limits and replay protection;
- the audit of every use;
- the model-input scanner;
- vault key rotation coverage;
- a restore drill.

**Out:**

- WebAuthn and passkeys, SMS or email codes, and CAPTCHA: these always go to a person;
- an export of secrets, which never happens;
- a mobile authenticator app.

## Owner prerequisites

- The consent text for enrolling a second factor, approved by the owner.
- Decide who may view codes for each shop (default: the Owner only).

## Slices

| Slice | Content | Key acceptance criteria |
|---|---|---|
| **8B.1 Tables and sealing** | `app.totp_accounts` (organization, shop, label, issuer, digits, period, algorithm, sealed secret, key ID, status, created by, consent reference) and `app.totp_events` (append-only: enrol, rotate, remove, view, inject, refuse). RLS: Owner and Admin of the organization; codes are never stored. | AC-8B.1.1: a direct SQL read shows ciphertext only; a secret copied to another shop's row fails to open. AC-8B.1.2: rotating the vault key re-wraps TOTP secrets with no downtime (same test as AC-4.2.2). |
| **8B.2 Enrolment** | Paste an `otpauth://` URI or secret, or upload a QR image. Decoded server-side; the image is held in memory only, never written. Consent and step-up required. The first code must be confirmed before the secret is saved. Several labelled accounts per shop. | AC-8B.2.1: a wrong confirmation code saves nothing. AC-8B.2.2: the QR image never touches disk or storage (file system and storage test). AC-8B.2.3: enrolment without the consent record or a recent step-up is refused. |
| **8B.3 Rotation, replacement, removal** | Rotate: the new secret replaces the old one only after its first code is confirmed. Removal destroys the ciphertext. All audited. | AC-8B.3.1: an interrupted rotation leaves the old secret working. AC-8B.3.2: after removal no code can be produced and the row holds no ciphertext. |
| **8B.4 Code view** | Shop page → "Authenticator": the current code with a countdown to the period end (server time), refreshed per period, only for users with `view_totp_code` on that shop and a recent step-up. No copy history, no caching (`Cache-Control: no-store`). | AC-8B.4.1: a user without the capability gets 404. AC-8B.4.2: each view writes one `totp_events` row without the code. AC-8B.4.3: more than 1 view per period or 10 per hour per user is refused with 429. |
| **8B.5 Injection into the browser worker** | The Secret Broker issues a grant bound to one execution, environment and field (TTL 60 s). The browser worker redeems it and types the code. The caller sees `injected: true`. One injection per account per period; 3 per 10 minutes per environment. | AC-8B.5.1: a second redemption of the same grant fails. AC-8B.5.2: a second injection in the same period waits for the next period (replay protection). AC-8B.5.3: only the `htco_browser` role can redeem. |
| **8B.6 LLM boundary** | No tool returns a code or a secret (registry check from 5.17). The model gateway's input scanner blocks any input that contains a live code or a secret's pattern. The structlog processor redacts by type. | AC-8B.6.1: a fixture prompt containing the current code is blocked before the provider call and recorded as a security finding. AC-8B.6.2: log capture shows no secret or code across the whole 8B test suite. |
| **8B.7 Drill and docs** | A restore drill includes `totp_accounts`; the threat model and the operations runbook (lost key, suspected exposure: remove, re-enrol, rotate at the provider). | AC-8B.7.1: a restored database plus the key ring produces the same codes; without the key ring, nothing opens. |
| **8B.8 Agent Secrets Vault** (added 2026-10-28, the owner's request) | A general vault for the credentials that AI employees and automations use, beyond a shop's sign-in (`private.shop_credentials`, AC-8A.4.5): API keys, service tokens and the user name and password of non-shop accounts. `app.agent_secrets` (organization, optional shop, label, kind `api_key`, `token`, `password` or `totp`, sealed value, key ID, status, rotation due date, created by) and the append-only `app.agent_secret_events`. **Envelope encryption** on the vault's key ring (one crypto scheme, ADR-0021). **Per-secret grants** (`app.agent_secret_grants`): which employee, automation or tool may use a secret, for which action and channel, set by the Owner with step-up. **Brokered injection only**: the Secret Broker (5.17) opens a value for one grant bound to one execution and one field, and the channel puts it into the outgoing HTTP header, the browser field or the tool's adapter; the agent or model sees only a reference and `injected: true`, never the value. **Rotation**: replace with a new value confirmed by a test call where the kind allows it, a due date with a reminder, and removal that destroys the ciphertext. TOTP accounts (8B.1) are the vault's `totp` kind: their codes are generated server-side by the Authenticator and injected (8B.5), never shown to a model (8B.6); the code view of 8B.4 is for people only. | AC-8B.8.1: no API response, log line, audit row, event, prompt or model input holds a secret's value (a seeded canary value is searched for across the whole 8B suite and the model gateway's recorded inputs). AC-8B.8.2: a use without a matching grant, or a grant redeemed twice or after its TTL, is refused before the value is opened, and each refusal is audited. AC-8B.8.3: an agent's tool call that uses an API key reaches a fake provider with the right header while the agent's transcript holds only the secret's reference. AC-8B.8.4: every create, grant, use, rotation and removal writes one `agent_secret_events` row and one audit record without the value; a secret past its rotation date raises one notification. AC-8B.8.5: the model gateway's input scanner (8B.6) blocks any input holding a vault value or its pattern. |
| **8B.9 Vault page** (added 2026-10-28) | Web step for 8B.8 (ER-UX-07): Admin › Secrets lists every secret (label, kind, shop, status, created by, last use, rotation due; never the value) with its grants and its events. The Owner adds a value through a write-only form with step-up, replaces, rotates or removes it, and grants or revokes its use per employee, automation and action. The shop-sign-in details of AC-8A.9.7 and the TOTP accounts of 8B.3 appear in the same list, managed on their own screens. | AC-8B.9.1: a saved value is never shown again, only replaced; the form never keeps it in browser storage. AC-8B.9.2: Admins see the list read-only, other roles see no section (negative tests); every change needs the Owner's step-up and is audited. |

**Cross-link: the Authenticator and the vault.** The internal TOTP authenticator of 8B.1 to 8B.7 is one kind of secret in the Agent Secrets Vault (8B.8): the same key ring, the same Secret Broker, the same grant and event model. Its codes are generated server-side and injected into the browser worker (8B.5); the model only ever learns that a code was injected (8B.6).

## Test plan

- PyOTP against RFC 6238 test vectors.
- Property tests for period boundaries and clock skew (±1 step accepted at confirmation only).
- pgTAP for RLS.
- Log-capture and scanner tests.
- Integration with Phase 8A's fake login page.

## Security checklist

- [ ] One crypto scheme (the vault), no new keys.
- [ ] ASVS v5 V6 (authentication) and V11 (cryptography) items for TOTP storage.
- [ ] Step-up for enrolment, view and removal.
- [ ] THREAT_MODEL updated.

## Demo

1. Enrol a test TOTP account from a QR image and confirm it.
2. Watch the code and its countdown in the dashboard.
3. Sign in to the test site through the browser worker with an injected code.
4. Show the audit trail without any code in it.

## Exit gate

- [ ] All AC-8B pass.
- [ ] The owner enrolled the pilot shop's Etsy TOTP, if he chooses to.

## Rollback

Disable the `authenticator` flag: no views or injections, and sign-ins become Human tasks. The secrets stay sealed until removed.

## Risks

| Risk | Mitigation |
|---|---|
| Vault key and database exposed together | Keys stay outside the database; the audit makes access visible; stored factors are listed per shop; rotate at the provider on suspicion |
| The owner relies on HTCO as his only authenticator | The enrolment screen recommends keeping a second device; recovery codes are not stored |

## Cost impact

None. PyOTP is MIT-licensed and runs in the existing API and browser worker.
