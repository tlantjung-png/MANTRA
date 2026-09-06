---
name: unattended
description: Run bounded unattended work headless or scheduled with provider fallback limits, resumable state, and an explicit escalation contract.
version: 1.0.0
user-invocable: true
---

# Unattended

## Prerequisites

- The repo's headless entrypoint present (`mantra-headless --config <cfg>
  --task <task.json>`, i.e. `core.main`); it refuses to run without a valid config and task file.
- Operator approval for anything that mutates repositories or publishes changes.

## Use When

Use for overnight runs, scheduled maintenance, batch fixes across many items, or
any work that must proceed without an operator watching each turn.

## Procedure

1. Choose the vehicle: a one-shot bounded run (headless `core.main` with a task
   JSON file) or a resumable work list via `/workflow` steps driven against the
   same workspace.
2. Set the bounds before starting: provider retry limit (default 1), max steps in
   the task config (`max_steps`), and a wall-clock budget.
3. Write the escalation contract down first: transient conditions (rate limit,
   timeout) may retry exactly once; auth failures, repeated identical failures, and
   denied operations stop the item. Preserve non-zero exits as failures, never
   retry them into false success.
4. Keep artifacts on disk: deliverables, logs, and a state record (session file)
   that survives the run, so a later session can reconstruct progress (pair with
   context-handoff for long runs).
5. Watchdog: run `git status`/`git diff --check` to confirm the tree is clean,
   and re-read the workspace memory before and after; compare rather than
   trusting exit codes alone.

## Verification

Report items completed/failed/skipped with evidence paths and every escalation
that fired. A run whose only evidence is "it exited zero" is unverified.

## Boundaries

No live-money paths, no force-pushes, no permission-policy changes in unattended
mode; confirmation-based gates stay confirmation-based. Scheduled tasks are
live-only operator state - registering or editing them requires approval.