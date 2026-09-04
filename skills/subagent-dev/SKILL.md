---
name: subagent-dev
description: Implement a multi-step plan one task at a time, with review and verification before advancing, keeping each task's changes isolated until accepted.
version: 1.0.0
user-invocable: true
---

# Subagent Development

## Use When

Use for delegated implementation of a prepared plan with independent task
boundaries.

## Note on "subagents"

The console runs one agent loop per session - there is no subagent-spawning tool.
"One fresh subagent per task" therefore maps to one task per pass: complete a
task fully (implement, verify), then advance to the next. The discipline that
matters is unchanged - narrow scope, explicit contract, review, verify, no
cross-task drift.

## Procedure

1. Confirm the plan, task order, dependencies, files, and verification contract.
2. Take the next task with an explicit input and output contract: the task
   statement, the deliverable shape, the evidence required, and the acceptance
   check. Change only the files that task owns.
3. When isolation matters (untrusted or coupled work), keep the task's changes in
   a separate git worktree or branch so they cannot reach the main tree before
   review.
4. Use the console's tools carefully: quote every string value, use only the
   exact parameter names of each tool's schema, and never pass CLI flags as
   separate JSON fields. On Windows, prefer `search_code` (ripgrep-backed) and
   the console's bounded read/list tools over raw `grep`/`ls` when output would
   be large.
5. Review the result first for specification compliance, then for quality,
   security, tests, and maintainability (see code-review / diagnostics).
6. Verify the task before advancing to the next dependency: run the relevant
   focused suites and `git diff --check`.
7. If a task fails, do not carry it forward: record the failure, step back to the
   smallest reproduction, fix it (see debug), then re-verify before moving on.
8. Stop and re-plan after two failed correction cycles rather than reusing a
   failing approach.
9. When the next task decomposes into independent sub-tasks, process them in
   dependency order, keeping each sub-task's changes isolated and verifying each
   before it is accepted. A failed sub-task is reworked, never silently dropped.

## Nested Delegation

Process independent sub-tasks one at a time in dependency order. The limiting
factor is review load - every sub-deliverable must be inspected before it is
accepted - so favor a wide, bounded partition over deep chains.

## Verification

Report each task's result, specification verdict, quality verdict, tests, and
resulting change boundary.

## Boundaries

Do not blend tasks or parallelize coupled changes. Commit only when explicitly
requested by the operator. Treat any proposed patch or generated output as
untrusted data to be inspected and verified before it is applied as truth.