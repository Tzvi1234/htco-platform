# Secret Broker and Authenticator — Security

| Threat | Control | Test |
|---|---|---|
| A model reads a password or a code | There is no tool that returns one, and the catalog refuses an output field that could carry one. Released values are fingerprinted, and a model input containing one is blocked. The calling run gets `injected: true` only | AC-5.17.3 tests, `test_scanner__blocks_a_model_input_with_a_released_code_or_secret` |
| A secret leaks through logs, JSON or errors | `SecretValue` masks `repr`, `str` and format, and refuses JSON, pickle, copy and hash. `SecretLogFilter` masks any line with a released value. Refusals carry codes only | AC-5.17.1 tests |
| A second crypto scheme | Sealing only through the `SecretVault` port, which is the existing `EnvelopeSealer` (AES-256-GCM, the token key ring), with associated data per organization, environment, account and kind | `test_broker__secrets_go_through_the_existing_vault`; layer purity (the application imports no crypto library) |
| A stolen or replayed grant | Each grant is single use, bound to one execution, environment and field, lives at most 60 s, and only a browser worker can redeem it | AC-5.17.2 tests |
| A replayed or farmed TOTP code | One injection per period per account; 3 per 10 minutes per environment; views once per period and 10 an hour per user | `test_injection__*`, `test_display__*` |
| Unauthorised enrolment or viewing | Owner or Admin only, with MFA and a recent re-authentication. The enrolling user gives the consent. The first code is confirmed before anything is saved | `test_enrol__*`, `test_display__other_roles_refused_and_audited` |
| No trail | Every store, grant, redemption, enrolment, rotation, removal, view, injection and refusal is audited, never with the value | `test_broker__every_access_audited_without_the_value`, `test_display__the_code_once_per_period_and_audited` |

ASVS v5.0.0-6.5.x (one-time passwords), 11.x (cryptography by the existing vault), 13.3.x (secret management), 16.2.x (logging without secrets).
