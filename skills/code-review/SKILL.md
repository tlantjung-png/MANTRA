---
name: code-review
description: Perform a strict maintainability and structural-quality review focused on correctness risks, lifecycle, boundaries, duplication, code growth, and the model-visible surface, reporting evidence-backed findings.
version: 1.1.0
user-invocable: true
---

# Code Review

## Use When

Use for a deep audit, maintainability review, structural review, or a request to find spaghetti, dead code, or abstraction problems.

## Procedure

1. Read the current change and its surrounding architecture. When a base is available, verify it and inspect the change scope against it before reading the diff.
2. Search for a simpler structure that deletes branches, wrappers, duplicated helpers, or misplaced ownership.
3. Inspect file growth, branching, boundaries, optionality, casts, state transitions, orchestration, and atomicity.
4. Trace callers before labeling code dead or redundant.
5. Apply the manual checks that code alone cannot show:
   - **Lifecycle and concurrency** — for async setup, callbacks, processes, or teardown: races before publication, cancellation during awaits, independent error reporting, callback containment, ownership before reentry, complete detach cleanup, and disposal to quiescence.
   - **Capability and consumer fit** — trace every current consumer; flag consumer-specific behavior leaking into a generic interface, and the inverse: a new public method whose only caller is one internal consumer is an unnecessary API expansion.
   - **Model perspective** — inspect the exact prompts, tool schemas, results, and diagnostics the model receives across affected modes; flag concepts outside its task.
   - **Enforcement** — follow every denial path to the operation that executes it; exercise direct and alternate callers that can bypass schemas, prompts, facades, wrappers, or listener ordering.
   - **Bounds cover the final operation** — probe tiny and exact limits, oversized single chunks, and multibyte text for byte limits.
   - **Real entry path** — tests must exercise the shipped loader, bin, worker, or subprocess, not a hand-mounted plugin.
   - **Test strength** — assertions fail on the intended regression and verify external state, logs, events, or disposal rather than restating the implementation.
6. Give every added or changed passage of prose, comments, prompts, or visible strings a semantic review: verify required coverage, accuracy, and placement against the owning code or behavior.

## Verification

Report each finding as defect, location, impact, and evidence. Place a localized defect inline on the tightest diff range; use a PR-level comment for cross-cutting concerns. Separate blockers from suggestions and omit issues already enforced by a green gate. Rank findings highest to lowest priority and end with a clear ship or no-ship verdict; a highest-priority finding requires the no-ship verdict.

## Boundaries

This is a maintainability and correctness review, not a substitute for the focused security or performance workflows. Do not approve merely because tests pass when the structure creates a clear correctness risk.
