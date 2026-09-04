---
name: debug
description: Reproduce a bug or failure, identify its root cause from the actual path, make the smallest safe correction, and prove the regression is gone.
version: 1.0.0
user-invocable: true
---

# Debug

## Prerequisites

- The console's file tools enforce read-before-edit: `edit_file` rejects any
  path you have not read this session, or whose content changed since the read.

## Use When

Use for crashes, failing tests, tracebacks, regressions, unexpected output, or
unexplained behavior.

## Procedure

1. Reproduce the exact failure and preserve the useful output.
2. Read the stack frames, callers, inputs, state transitions, and external
   boundaries.
3. State one falsifiable root-cause hypothesis.
4. Before editing, read the file with `read_file` (a fresh read records the
   current content in the edit ledger). Use an exact `old_string` from that read;
   re-read if the file was touched since the last read. An unread or stale edit is
   rejected by the tool.
5. Make one minimal correction and rerun the exact failure plus nearby contracts.
6. Remove temporary probes and report what changed.

## Verification

Report the reproduction, root cause, exact location, correction, regression proof,
and any unavailable environment check.

## Boundaries

Stop after three unsuccessful correction attempts and question the model or
architecture instead of guessing repeatedly. Do not mask failures with broad
fallbacks.