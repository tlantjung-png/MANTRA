---
name: improve-coverage
description: Add meaningful tests for existing behavior by prioritizing high-risk boundaries, regressions, failures, and untested contracts rather than chasing a percentage.
version: 1.0.0
user-invocable: true
---

# Improve Coverage

## Use When

Use when behavior lacks tests, a boundary is unverified, or a regression should be pinned.

## Procedure

1. Map critical paths, external boundaries, failure modes, and current test gaps.
2. Prioritize security, data loss, concurrency, status propagation, and user-visible contracts.
3. Write behavior-focused fixtures that fail for the missing behavior and pass for the intended behavior.
4. Run the focused suite and then the relevant project gate.
5. Record deliberate omissions and remaining environmental gaps.

## Verification

New tests must exercise real behavior, avoid circular assertions, and pass independently of live machine state when isolation is possible.

## Boundaries

Do not weaken or delete tests to obtain a green result. If a missing test exposes a bug, report the bug before expanding the suite.
