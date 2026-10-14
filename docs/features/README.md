# Features

Every feature has its own folder here, created from [`_template/`](_template/) at the start of its first slice and kept true in every later pull request. The [`feature-docs`](../../.claude/skills/feature-docs/SKILL.md) skill explains what goes in each file.

| File | Answers |
|---|---|
| `overview.md` | What does it do, for whom, and how do we know it works (acceptance criteria)? |
| `design.md` | How is it built: layers, contracts, data, flows, decisions? |
| `tests.md` | Which tests cover which criteria, and what did real verification show? |
| `security.md` | What can go wrong, what stops it, which ASVS requirements are met? |
| `operations.md` | How is it configured, monitored, fixed and rolled back? |

## Index

| Feature | Phase | Status |
|---|---|---|
| [platform-skeleton](platform-skeleton/overview.md) | 1 | In progress |
| [identity-access](identity-access/overview.md) | 2 | In progress |
| [living-office](living-office/overview.md) | 2 (foundation), 5–19 | Foundation complete |
