---
name: tdd
description: Develop a feature or regression test-first by proving the expected failure, making the smallest implementation, refactoring safely, and running broader relevant checks.
version: 1.0.0
user-invocable: true
---

# Test-Driven Development

## Prerequisites

- The console's file tools enforce read-before-edit: `edit_file` rejects any
  path you have not read this session, or whose content changed since the read.
  No separate helper scripts are needed.

## Use When

Use for new behavior, bug fixes, regression tests, or explicit test-first work.

## Procedure

1. Define the observable behavior and boundary cases.
2. Add a focused test that fails for the missing behavior and confirm the failure
   is meaningful.
3. Before editing, read the file with `read_file` (a fresh read records the
   current content in the edit ledger). Use an exact `old_string` from that read;
   if the file changed since, re-read first - a stale `old_string` fails the edit,
   and an unread edit is rejected outright.
4. Implement the smallest change that makes the test pass.
5. Refactor only after green, preserving the behavior contract.
6. Run the focused suite and broader project verification.

## Verification

Report the test location, the expected red result, the implementation result, the
green result, and the refactoring or remaining gap.

## Boundaries

Do not write production behavior before demonstrating the failing test. Reject
circular, over-mocked, or happy-path-only tests. Use the improve-coverage skill
for existing-code backfills when test-first implementation is not appropriate.