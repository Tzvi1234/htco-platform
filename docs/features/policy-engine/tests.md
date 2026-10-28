# Policy engine — Tests

| Level | What is covered | Location |
|---|---|---|
| Domain | Each reason's verdict; decision versions; the document rules (catalogue, autonomy, pre-approvals, names, actor ids); read-only mappings; strict JSON round trip | `tests/domain/test_policy.py` |
| Application (engine) | Every cell of PERMISSION_MATRIX section 1 for users; the identity table by autonomy level × side effect × risk; unknown capability and actor; no policy; own grants and narrowing (AC-7.2.4); advisors; tenancy; the baseline from the real registry | `tests/application/test_policy_engine.py` |
| Application (service) | Next version on publish; old decisions keep their version; recorded decisions; the baseline is published once; revert as a new version; only a stepped-up Owner publishes; the publish audit | `tests/application/test_policies.py` |
| Integration (Postgres) | Six concurrent publishes give versions 1–6, each with its audit; document round trip; baseline once; versions in recorded decisions; capability cut to 200 characters; the worker's scope | `tests/integration/test_policy_store.py` |
| Database (pgTAP) | Version uniqueness and bounds; append-only tables; reason ↔ verdict and version checks; foreign key to the version; Owner and Admin read their own organization; Operations reads nothing; no member writes; the worker's scope | `supabase/tests/database/37_policies.test.sql` |

| Criterion | Tests |
|---|---|
| AC-7.2.1 | `test_unknown_capability__is_denied`, `test_unknown_actor__is_denied`, `test_user_without_a_principal__is_an_unknown_actor`, `test_no_policy__denies_every_actor` |
| AC-7.2.2 | `test_user__every_cell_of_the_human_matrix`, `test_identity__the_permission_table`, `test_identity__the_table_covers_every_level_and_every_kind_of_capability` |
| AC-7.2.3 | `test_publish__a_change_creates_the_next_version`, `test_decide__old_decisions_keep_their_version`, `test_decisions__keep_the_version_they_were_made_under`, pgTAP 37 |
| AC-7.2.4 | `test_automation__an_owner_attached_to_the_request_never_widens_its_grants`, `test_employee__is_limited_to_the_originating_users_shops`, `test_employee__never_gets_a_capability_from_the_originating_user`, `test_employee__without_an_originating_user_reaches_no_shop`, and the others marked AC-7.2.4 |
| AC-7.2.5 | `test_advisor__every_request_is_denied`, `test_advisor__named_like_a_granted_identity_is_still_denied` |
