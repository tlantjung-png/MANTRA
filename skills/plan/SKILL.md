---
name: plan
description: Produce a self-contained, actionable implementation plan with architecture, exact paths, bite-sized test-first tasks, risks, and final verification without implementing it.
version: 1.0.0
user-invocable: true
---

# Plan

## Use When

Use for feature specifications, architecture breakdowns, multi-step work, or requests for a plan before implementation.

## Procedure

1. Clarify the outcome, constraints, scope, and non-goals; interview for larger features when needed.
2. Read the relevant source and existing patterns before choosing a design.
3. Write a dated plan under the project's plan location with exact files, data shapes, dependencies, risks, and test-first tasks. When the host plan mode is active, align the plan with the native plan file so the review and approval flow work; map the plan's task conventions to the native plan-file location.
4. Include expected results and one final end-to-end verification step for every task.
5. After plan approval, offer implementation; when the host plan mode gates edits, leave plan mode only through the native approval flow.

## Verification

The plan must be executable by another engineer without redoing discovery. It must distinguish current behavior, proposed behavior, and unresolved decisions.

## Boundaries

Plan mode does not modify production files, run mutating commands, commit, or push. Skip a plan for a trivial one-line change.
