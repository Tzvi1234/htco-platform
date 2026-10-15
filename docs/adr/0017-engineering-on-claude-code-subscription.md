# ADR-0017: The Engineering department runs on Claude Code with the owner's subscription

| | |
|---|---|
| **Status** | Accepted (owner request, 2026-10-15) |
| **Date** | 2026-10-15 |
| **Related** | [ADR-0012](0012-living-office-organization.md) (living office), [ADR-0015](0015-decision-advisor-and-routing.md) (model routing), [Phase 19](../plan/phases/phase-19-engineering.md), D-08 |

## Context

Phase 19 activates three engineering employees (Alex, Amir, Iris) who change the system's own code. As planned, they would run in our agent runtime on API-billed models, like every other employee. The owner asked that the Engineering department instead run on his Claude subscription to save money. He accepts that this limits their autonomy compared with the other employees, and asked for this only if it is possible and allowed.

Research on 2026-10-15 used primary sources:

- **It is possible.**
  - `claude setup-token` creates a one-year OAuth token "for CI pipelines, scripts". It requires a Pro, Max, Team or Enterprise plan.
  - `anthropics/claude-code-action` accepts that token as `claude_code_oauth_token`, and its runs "use your Claude subscription instead of API billing".
  - Claude Code headless (`claude -p`) and cloud Routines also run on a subscription.
  - Sources: code.claude.com/docs/en/authentication, /github-actions, /headless, /routines.
- **It is allowed within limits.**
  - Subscription OAuth is for the subscriber's own, ordinary use of the unmodified Claude Code (code.claude.com/docs/en/legal-and-compliance).
  - Developers building products, including any Agent SDK use, must use API keys.
  - Nobody may share the account or credentials, or route other users' requests through a Free, Pro or Max plan (Consumer Terms §2–3; Agent SDK overview).
  - Programmatic subscription use has changed twice in 2026, so it must be swappable.
- **Limits:**
  - The 5-hour and weekly windows are shared with the owner's own Claude use.
  - Pro is too small. Max (preferably 20x) is the floor.
  - When a window is exhausted, new runs fail until it resets, unless extra usage is enabled.

## Decision

1. **The engineering employees are Claude Code, not our runtime.**
   - Alex, Amir and Iris keep their registry sheets: identity, ownership, rules and autonomy L1.
   - Their work runs in the unmodified Claude Code CLI through `anthropics/claude-code-action` in GitHub Actions, authenticated with the owner's `CLAUDE_CODE_OAUTH_TOKEN`. The token is stored only as a GitHub Actions secret, never in code or logs, and rotated before it expires.
2. **GitHub is the bus.**
   - The office opens an engineering request (`engineering_requests`). Its GitHub issue carries a label that routes it (`eng:alex`, `eng:amir`, `eng:iris`) and the request's context.
   - The workflow runs Claude Code with the employee's instructions (from the registry, rendered into the prompt and `CLAUDE.md`). Claude pushes a `claude/*` branch and opens a pull request.
   - Results flow back through webhooks into the inbox (pull request, CI and comment events). From there they reach the office as messages, so engineering talks to the other employees.
   - Other employees comment on the issue or pull request to ask or answer. Claude Code reads the thread on its next run.
3. **The owner stays in control.** Branch protection, required reviews, CI, test-integrity and the owner's release gate apply unchanged. No autonomous merges or deployments. `--max-turns`, workflow timeouts and `concurrency` limit each run to one or two jobs at a time, which protects the owner's own usage windows.
4. **Scheduled work** (nightly maintenance, dependency updates) may use Claude Code Routines on the same subscription. The office can fire a routine through its HTTP trigger.
5. **Swappable credential.** One setting decides whether a run uses `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` (or the API credits included with Max). If Anthropic restricts programmatic subscription use, the department moves to API billing with one configuration change.
6. **Never product-facing.**
   - The subscription is used only by the owner, for the owner's own repositories.
   - No Agent SDK code inside HTCO OS runs on it. The rest of the office keeps API keys, per ADR-0015.
   - If HTCO OS ever serves other businesses, their engineering work uses API billing.

## Consequences

- Engineering model cost moves from per-token billing to the flat subscription. The owner needs Max, and his own interactive limits are shared with the department.
- Engineering runs are one-shot and stateless. Context lives in the issue, the pull request, `CLAUDE.md` and the registry. Long investigations span several runs.
- When a usage window is exhausted, runs fail. The orchestrator reads that failure, waits for the reset, retries, and tells the owner when work is delayed.
- Phase 19's GitHub App (D-08) is the Claude GitHub App, or a custom app with only Contents, Issues and Pull requests. Commits made through Routines appear as the owner.
- Prompt injection: issue and pull request text written by other employees or outsiders is data. The workflow limits `--allowedTools`, and only the office's bot may trigger it (`allowed_bots`).
- Owner prerequisites (added to Phase 19):
  - a Max subscription;
  - running `claude setup-token` himself and saving it with `gh secret set CLAUDE_CODE_OAUTH_TOKEN`;
  - installing the GitHub App.
