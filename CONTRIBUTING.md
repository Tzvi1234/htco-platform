# Contributing

HTCO is developed by H.T.Z Studios with AI coding agents. External contributions are not accepted at this time. This guide describes how changes are made, for everyone who works in the repository.

## Workflow

1. **Start from the plan.** Every change belongs to a slice in [`docs/plan/phases/`](docs/plan/phases/) or to an issue.
2. **Branch** from `main`: `feat/<scope>-<short-name>`, `fix/…`, `docs/…`, `chore/…`, `refactor/…`, `test/…`.
3. **Write acceptance criteria**, then **failing tests**, then the code ([TESTING_STRATEGY.md](docs/engineering/TESTING_STRATEGY.md)).
4. **Verify for real** using the [feature-verification](.claude/skills/feature-verification/SKILL.md) procedure and record the evidence.
5. **Update documentation** in the same pull request ([feature-docs](.claude/skills/feature-docs/SKILL.md)).
6. **Open a pull request** with a Conventional Commit title (`feat(orders): ingest receipts idempotently`) and complete the template, including the security checklist.
7. **Merge** by squash after CI is green and the review is done. `main` is protected.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary` with types `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`. Breaking changes add `!` and a `BREAKING CHANGE:` footer.

## Definition of Done

See [ENGINEERING_RULES.md §15](docs/engineering/ENGINEERING_RULES.md#15-definition-of-ready-and-definition-of-done).

## Code of conduct

Be respectful and constructive in issues, reviews and commits.
