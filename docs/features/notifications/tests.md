# Notifications — Tests

| Level | What is covered | Location |
|---|---|---|
| Domain and application | Allowlist, security emails always on, one notification and email per recipient over replays, preferences, every template in both languages with identifiers only, tampered parameters refused, delivery outcomes, security notices, failures never blocking | `tests/application/test_notifications.py` |
| Adapters | Resend on a mock transport: idempotency key, headers, temporary and permanent statuses, network errors, hidden key; the log adapter logs no address | `tests/integrations/test_email_adapters.py` |
| API | The inbox and its unread count, mark read; the incident center routes | `tests/api/test_incident_routes.py` |
| Worker | Subscribers and the email destination wired; Resend only with its key | `tests/worker/test_notification_wiring.py` |
| Integration (Postgres) | As the worker: one notification and one email per owner over three runs; the address at send time | `tests/integration/test_notification_store.py` |
| Database (pgTAP) | Uniqueness, no address in errors, own rows only, admins see deliveries, the worker's functions and scope | `supabase/tests/database/25_notifications.test.sql` |

| Criterion | Tests |
|---|---|
| AC-6.4.1 | `test_ac_6_4_1_one_incident_one_email_per_recipient_even_when_run_twice`, `test_ac_6_4_1_the_email_job_twice_sends_one_email`, `test_resend_gets_the_outbox_key_as_its_idempotency_key`, `test_worker__one_notification_and_one_email_per_owner_however_often` |
| AC-6.4.2 | `test_ac_6_4_2_only_identifiers_pass`, `test_ac_6_4_2_every_template_renders_with_identifiers_only`, `test_templates_refuse_buyer_data_from_a_tampered_outbox` |
| AC-6.4.3 | `test_ac_6_4_3_a_temporary_failure_raises_so_the_outbox_retries_with_backoff`, `test_ac_6_4_3_a_permanent_failure_is_recorded_for_the_admin_and_not_retried`, `test_resend_busy_or_down_is_temporary`, `test_resend_refusing_the_message_is_permanent_without_the_address`, pgTAP "the owner sees the failed delivery"; the admin view `GET /v1/operations/email-failures`: `test_ac_6_4_3_the_record_names_the_shop_incident_and_order_for_the_admin_view`, `tests/application/test_email_failures.py`, `test_email_failures__the_admin_view_with_a_masked_recipient` (API), `test_admin_view__a_permanent_failure_with_its_links_and_a_masked_address` (integration), pgTAP `34_email_delivery_admin_view` |
| AC-6.4.4 | `test_ac_6_4_4_a_sign_in_from_a_new_device_sends_one_email_to_that_user`, `test_ac_6_4_4_a_known_device_or_a_first_sign_in_sends_nothing`, `test_ac_6_4_4_mfa_changes_and_shop_connections_notify_the_owners` |
