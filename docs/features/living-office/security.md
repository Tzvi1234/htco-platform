# Living office — Security

## Threats (foundation)

| Element | Threat | Control | Test |
|---|---|---|---|
| Tool calls | Elevation of privilege: an employee reaches a tool it was not granted, or acts while not production-ready | Deny by default in `decide_tool_access`; readiness gate; critical and external tools need approval | `test_tool_access.py` |
| Automation triggers | Elevation of privilege and replay: an employee triggers a workflow outside its grants, or a trigger repeats a business action | Engine checks grants, trigger kind and readiness; executions unique per idempotency key | `test_automation_engine.py` |
| Registry | Tampering: a grant or policy widened without review | Reviewed pull requests; the registry refuses unknown tools and automations and non-agent-tool grants; employees have only `registry.read` | `test_registry.py`, `test_registry_files.py` |
| Decision Advisor | Elevation of privilege: advice used as authorization | Access functions take no advisor input; answers schema-validated | `test_advisor_answers__never_change_authorization` |
| Model routing | Information disclosure: personal data sent to a model or provider not cleared for it | Policy's maximum data class; models' cleared data class; refusal instead of downgrade | `test_privacy__*` |
| Activity records | Information disclosure: reasoning, secrets or PII stored as records | Summaries capped at 500 characters; records hold business outcomes only (ER-AI-09, ER-AI-16) | `test_activity_record__summary_is_a_short_business_summary` |

## ASVS 5.0 requirements touched

| Requirement | How it is met |
|---|---|
| v5.0.0-8.2.1 | Every tool and automation needs an explicit grant |
| v5.0.0-8.3.3 | Employees act with their own service identity and grants, never a user's |
| v5.0.0-16.2.1 | Activity records carry actor, time, correlation and outcome |
