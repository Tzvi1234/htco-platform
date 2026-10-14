# Platform skeleton — Tests and verification

## Test plan

| Level | What is covered | Location |
|---|---|---|
| Architecture | Layer purity (allow-list), workspace packages import from this checkout; import-linter contracts run in `just arch` | `tests/architecture/` |
| Compatibility | LangGraph two-node graph with an in-memory checkpointer on Python 3.14 | `tests/agent_runtime/test_langgraph_compat.py` |
| Tooling | Conventional Commits checker (headers, Git-generated messages, message files, titles) | `tests/tools/test_conventional_commit.py` |
| Tooling | SHA-pin checker (pinned, local and digest references pass; tags, branches, short SHAs and image tags fail) | `tests/tools/test_check_action_pins.py` |
| Tooling | Test-integrity checker (diff parsing, skip markers, removed tests, changed assertions, thresholds, quality-gate settings, the "Test changes" section) | `tests/tools/test_test_integrity.py` |
| CI | The pipeline itself, on GitHub | `.github/workflows/ci.yml` (verification below) |

## Acceptance criteria coverage

| Acceptance criterion | Tests |
|---|---|
| AC-1.1.1 | The whole suite through `just check` (manual run below; CI from slice 1.4) |
| AC-1.1.2 | `test_domain__imports__only_the_standard_library`, `test_application__imports__only_the_standard_library_and_the_domain`, `test_find_forbidden_imports__module_importing_fastapi__is_reported`; import-linter `dependency-rule` |
| AC-1.1.3 | `test_runtime__python_version__is_3_14`, `test_langgraph__two_node_graph_with_memory_checkpointer__runs_and_checkpoints` |
| AC-1.1.4 | The `test` recipe's per-layer `coverage report --fail-under` lines (manual run below) |
| AC-1.4.1 | The `ci-gate` job (verified on GitHub, below) |
| AC-1.4.2 | `test_main__skip_added_without_explanation__fails`, `test_main__skip_added_with_explanation__passes`, `test_diff_findings__skip_or_xfail_added_in_tests__is_reported` |
| AC-1.4.3 | `test_unpinned_uses__*`, `test_main__workflow_with_unpinned_action__fails_naming_file_and_line`; runs in the `workflows` job |

## Edge cases considered

- Import forms: `import x.y`, `from x import y`, imports inside functions and inside `TYPE_CHECKING` blocks are all reported; relative imports and `__future__` are allowed (parametrized tests).
- Nested sub-packages are scanned.
- Commit headers: empty, whitespace-only subject, missing space after the colon, empty or upper-case scope, unknown or capitalized type, exactly 100 characters (accepted) and 101 (rejected); Git-generated merge, revert, fixup and squash headers; comment lines in message files.
- Diff parsing: removed lines that start with `--` are not mistaken for file headers; tests moved between files and assertions moved unchanged are not findings; deleted test files report each test.
- "Test changes" section: missing, template comment only, `None`, `None.`, `N/A`, Windows line endings, level-3 heading.
- Action references: quoted values, reusable workflows, local actions, Docker digests vs tags, upper-case SHAs, commented-out lines.
- Not applicable yet: time, money, tenancy, concurrency (no business code).

## Verification evidence

### Verification — 1.1 — 2026-10-14

| Acceptance criterion | Covered by tests | Verified for real | Result |
|---|---|---|---|
| AC-1.1.1 | Whole suite | `uv run just check` on a fresh clone (Windows 11, uv 0.13.0, Python 3.14.8): format, lint, mypy strict clean, 4 import-linter contracts kept, 51 tests passed, coverage floors met | Pass |
| AC-1.1.2 | Purity tests | Added `import htco_api` to `htco_domain`: `just arch` failed with `Dependency rule: composition roots -> adapters -> application -> domain BROKEN` (`htco_domain -> htco_api (l.8)`). Added `import fastapi` alone: import-linter kept, purity test failed with `Broken contract: Domain imports only the standard library` / `htco_domain/__init__.py:7 imports 'fastapi'`. Both reverted. | Pass |
| AC-1.1.3 | Smoke tests | Ran on CPython 3.14.8 with langgraph 1.2.14 and langgraph-checkpoint 4.2.0 | Pass |
| AC-1.1.4 | — | Added an untested function to `htco_domain`: `just test` failed (`total of 17 is less than fail-under=80`); the domain report alone failed with `fail-under=95`. Reverted. | Pass |

**Adversarial checks run:** forbidden imports in the domain (outer layer and third-party); uncovered domain code; a commit with the message "updated some stuff" was rejected by the `commit-msg` hook; `prek run --all-files` with gitleaks found no secrets.
**Observed:** not applicable (no running service in this slice).
**Deployed check:** not applicable.
**Open issues:** none.
