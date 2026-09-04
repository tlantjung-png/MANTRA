---
name: simplify
description: Reduce complexity and duplication without changing observable behavior by classifying consumers, proving each candidate with evidence, and making a minimal verified change.
version: 1.1.0
user-invocable: true
---

# Simplify

## Prerequisites

- The console's file tools enforce read-before-edit: `edit_file` rejects any
  path you have not read this session, or whose content changed since the read.

## Use When

Use for cleanup, refactoring, dead code, redundant logic, or over-engineered
flows.

## Procedure

1. Inspect callers, history, and the current behavior before deleting anything.
2. Classify each candidate's consumers before labeling it dead:
   - **Production** - `src`, runtime scripts, loader and config paths: a deletion
     here is a feature decision, not a cleanup.
   - **Non-production** - tests, docs, snapshots, generated outputs: behavior they
     pin may still be load-bearing.
   - **Ambiguous** - examples and scripts that may be product smoke paths: inspect
     usage before classifying.
3. Prefer strong candidates: a public method, event, config knob, or package with
   no production consumer; two representations mirroring the same fact; a seam no
   consumer uses; speculative product generality; or hand-rolled code where a
   stdlib feature already exists (weigh net deletion: implementation plus
   dedicated tests plus docs, minus the glue that remains).
4. Route small cleanups to the session todo list (`/todo add`) and durable
   decisions to the workspace memory; do not manufacture a note for a typo.
5. Identify a smaller ownership boundary or a source of truth that removes
   concepts rather than relocating them.
6. Reconcile only non-conflicting, clearly safe changes and classify them by risk.
7. Before any edit, read the file with `read_file` (a fresh read records the
   current content in the edit ledger) and use an exact `old_string` from that
   read; re-read on a stale or missing read.
8. Run tests and review the final diff for behavior preservation.

## Verification

Report what was removed, consolidated, deferred, or left in place and why. Confirm
that the final change is smaller or easier to reason about without changing
behavior.

## Boundaries

Do not delete code whose purpose is unverified. Treat intentionally documented
dual backends as settled unless the evidence beats the recorded rationale. Flag
risky changes involving money paths, caches, public APIs, or concurrency for
operator decision.