---
name: check-work
description: Independently verify completed work by reconstructing the request, inspecting the current state, selecting the narrowest relevant evidence, running the available checks, and reducing all check results into one structured verdict.
version: 2.0.0
user-invocable: true
---

# Check Work

## Use When

Use after implementation, review, documentation, operational, or research work when an independent completion check is required.

## Procedure

1. Restate every requested deliverable as a concrete checklist.
2. Reconstruct what was actually attempted and identify failed or skipped actions.
3. Inspect the current files, diff, recent history, and repository rules instead of trusting prior claims.
4. Verify the outgoing scope against a verified base ref (never a guessed or un-fetched one) and select evidence by what the diff can affect:
   - **Package or script behavior** — the owning test file or focused test name; add adjacent tests only when a shared contract changes.
   - **Docs or comments** — the documentation checks; full lint when the doc workflow requires it.
   - **Model-, editor-, or terminal-visible output** — the focused snapshot or runnable-example scenario that owns the output.
   - **Manifests, exports, build config, or bin entries** — the build, relevant hygiene checks, and the owning built-artifact smoke.
   - Prefer the narrowest check that would fail for the regression; leave repository-wide coverage to CI unless the change is genuinely cross-cutting.
5. When coverage tooling applies (for example vitest `related` plus `--coverage.include` in JavaScript projects), name both the owning tests and the source scope; do not lower thresholds or narrow the include to hide an uncovered affected file. Coverage-diff tooling cannot discover behavior reached only through configuration, dynamic loading, subprocesses, workers, or external providers — select those owners explicitly.
6. Run the build, tests, diagnostics, and focused edge checks that the project defines.
7. Collect every check's result, then run the **Reduce** phase below to reconcile all results into one structured verdict.
8. Confirm the deliverable is materialized on disk, such as a file or a committed change, not only relayed in chat. Use the deliverable checker with the task's stated deliverable paths where they are known.
9. Review correctness, adequacy, excess, regressions, and edge cases.
10. If a check fails, describe the exact issue and repeat verification after a correction, up to three cycles.

## History-Rewrite Protection

Before a rewritten push, fetch the current remote branch and record its exact OID, then publish with `--force-with-lease=<branch>:<observed-oid>`; raw `--force` is never allowed. After any rewritten push, fetch the live heads again and re-audit review threads, approvals, mergeability, and checks — commit hashes from before the rewrite are not current evidence.

## Verification

End with the structured verdict from Reduce: area-level status for each check area, overall verdict (pass / fail / indeterminate), and any contradictions that need resolution. Pass only when every area passes. Fail when any area fails. Indeterminate when any area could not be verified and no area failed. Do not manually repeat a check that already passed merely because a commit or push follows.

## Reduce

Multiple checks run independently — build, tests, diagnostics, edge cases. Each produces its own pass/fail/skip result. The final verdict must reconcile all of them. Raw check output makes it easy to miss contradictions: build passes but a test fails, diagnostics are clean but an edge check fails. Reduce makes the overall state unambiguous.

### Step 1 — Validate

- Confirm each check actually ran. A check that was skipped, timed out, or crashed did not produce a valid result — mark it as indeterminate, not as pass.
- Verify the output is current. A cached or stale check result is not evidence.

### Step 2 — Normalize

- Convert every check result to a single shape: check name, area (build / test / diagnostic / edge), status (pass / fail / skip / error), evidence reference.
- Normalize severity: a test failure is a blocker; a diagnostic warning may be pre-existing; a skip is not a failure but must be acknowledged.

### Step 3 — Group

- Group results by area. Build results together, test results together, diagnostics together, edge cases together.
- Within each area, identify the dominant status. If 3 of 4 test suites pass but 1 fails, the test area is failed — the 3 passes do not override the 1 failure.
- Cross-reference areas. If the build passes but tests in the same component fail, group these as a contradiction that needs explanation (test isolation issue, environmental difference, stale build artifact).

### Step 4 — Surface Structure

- **Cross-check agreement**: When all areas agree (all pass or all fail), the verdict is unambiguous.
- **Cross-check contradiction**: When one area passes and another fails, the contradiction itself is the finding. Surface it explicitly — do not let the pass silently override the fail or vice versa.
- **Indeterminate checks**: Any check that could not produce a result reduces overall confidence. The verdict must note that some areas could not be verified.
- **Rejection log**: Any check that was skipped or errored, with the reason. This prevents silent gaps in coverage.

### Step 5 — Deliver

The Reduce phase produces:

1. **Area-level verdict** — pass, fail, or indeterminate for each area (build, test, diagnostic, edge).
2. **Overall verdict** — pass only when every area passes. Fail when any area fails. Indeterminate when any area could not be verified and no area failed.
3. **Contradiction report** — any cross-check disagreements that need explanation.
4. **Rejection log** — any check that did not produce a valid result.

## Boundaries

Passing tests are evidence, not proof that every requirement was met. Do not invent a build or runtime result when a tool is unavailable. Do not push and hope CI differs: stop and fix or explain the blocker first.
