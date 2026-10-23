# Secret Broker and Authenticator — Tests

| Criterion | Tests |
|---|---|
| AC-5.17.1 | `tests/domain/test_secrets.py::test_secret_value__never_shows_in_repr_str_format_or_errors` (Hypothesis), `::test_secret_value__never_serializes` (JSON, pickle, copy, hash, `asdict`), `::test_secret_value__not_in_a_log_record`; `tests/application/test_secrets.py::test_log_filter__masks_a_line_with_a_released_secret` (log capture), `::test_errors__a_refusal_never_carries_the_value`, `::test_scanner__blocks_a_model_input_with_a_released_code_or_secret`, `::test_broker__every_access_audited_without_the_value`, `tests/integrations/test_totp.py::test_parse__a_refusal_never_echoes_the_secret` |
| AC-5.17.2 | `test_grant__redeemed_once_by_a_browser_worker`, `test_grant__expires_within_60_seconds`, `test_grant__bound_to_its_execution_and_environment`, `test_redeem__anyone_but_a_browser_worker__refused`; `tests/domain/test_secrets.py::test_grant__lives_at_most_60_seconds`, `::test_grant__bound_to_its_environment_and_one_field` |
| AC-5.17.3 | `test_secret_fields__found_anywhere_in_an_output`, `test_secret_fields__ordinary_fields_pass`, `test_registry__no_tool_and_no_action_output_can_carry_a_secret`, `test_registry__an_action_whose_output_carries_a_secret__refused`, `test_secret_tool_names__a_tool_that_returns_a_code__found` |
| Authenticator controls | `test_enrol__*` (first code confirmed, MFA, step-up, consent), `test_display__*` (once per period, 10 an hour, roles), `test_injection__once_per_period_replay_refused`, `test_injection__at_most_three_per_ten_minutes_per_environment`, `test_rotate__*`, `test_remove__destroys_the_ciphertext` |
| PyOTP adapter | `tests/integrations/test_totp.py` (RFC 6238 appendix B vectors, drift, otpauth parsing, refusals) |

Verification: local lint, types, architecture and these tests; the full suite in CI.
