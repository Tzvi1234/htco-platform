# Activity records — Tests

| Level | What is covered | Location |
|---|---|---|
| Domain | The rules of `ActivityRecord` | `tests/domain/test_lifecycles.py` |
| Application | Templates and identifier parameters; free text refused (a direct `Summary`, a string summary, prose or markup parameters); naive time, missing or long correlation ID; action names | `tests/application/test_activity.py` |
| Integration (Postgres) | Every field and the template stored, in its month's partition; rolled back with the failed change; only an `ActivityEntry` accepted; no update or delete for the owner or the worker; the worker's organization; cascade on organization removal | `tests/integration/test_activity_store.py` |
| Database (pgTAP) | Partitioned table, RLS on every partition, routing by month and default partition; domain rules as checks; foreign key to the shop's organization; append-only; organization and shop visibility; no direct member writes or partition reads; the worker's scope | `supabase/tests/database/41_activity_records.test.sql` |

| Criterion | Tests |
|---|---|
| AC-7.7.2 | `test_entry__a_naive_timestamp_is_refused`, `test_entry__a_missing_correlation_id_is_refused`, `test_records__cannot_be_changed_or_deleted`, pgTAP 41 (summary length, correlation ID, append-only) |
| AC-7.7.3 | pgTAP 41: "the Owner sees every record of their organization", "a shop role sees organization-wide records and its granted shop's only", "organization B sees only its own record" |
| AC-7.7.4 | `test_summarize__a_parameter_that_is_not_an_identifier_is_refused`, `test_summary__cannot_be_built_from_free_text`, `test_entry__a_plain_string_summary_is_refused`, `test_append__refuses_anything_but_an_entry`, pgTAP 41 ("a summary names the code template") |
