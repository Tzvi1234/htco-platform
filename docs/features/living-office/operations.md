# Living office — Operations

Nothing runs in production yet.

## Changing the office

1. Edit the document in `registry/` (an employee's grants or routing policy, an automation's specification, a tool's definition).
2. Run `uv run just registry` and `uv run just check`.
3. Open a pull request. Any change to an employee's autonomy, tools, automations or budget, or to a tool's risk, needs the owner's approval in the pull request. Activating an employee or automation (`ready`, `active`) needs every section defined and the evaluation report linked.

## Summary

`uv run just registry` prints the count of employees (and how many are production-ready), automations, tools and advisors.
