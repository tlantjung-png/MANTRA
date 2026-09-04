---
name: refine
description: Convert completed work into durable memory or skill improvements through an evidence-backed, reversible checkpoint and an honest nothing-to-save outcome.
version: 1.1.0
user-invocable: true
---

# Refine

## Use When

Use at turn end or manually when a completed phase may contain a durable lesson.

## Procedure

1. Compare Git state and log growth with the previous refinement checkpoint
   (`git diff`, `git status`, recent `logs/*.jsonl` tail).
2. Leave a brief only when new material exists; a stable state is a valid no-op.
3. Begin a refinement by snapshotting the workspace memory
   (`<workspace>/.mantra/memory.md`) and any files you will change.
4. Patch existing durable entries, place facts in the correct file (memory,
   known-failure registry, a skill), and exclude one-off identifiers and temporary
   errors.
5. Restate every saved lesson from the repository's vantage: a durable entry must
   read correctly at HEAD with no session transcript available. Do not save
   session-vantage narration, review choreography, or hedged planning residue;
   promote deferred work to the session todo list (`/todo add`) or a stated bound
   instead.
6. Complete the refinement with an evidence record, then remove the brief.
7. Undo a bad refinement by restoring the snapshots and recording the rollback.

## Verification

Refinement must be evidence-backed, reversible, logged, and allowed to produce no
saved lesson. Confirm the snapshot, completion record, and cleanup.

## Boundaries

Never store secrets or duplicate facts. Do not turn a workaround into a default
process. Undo covers only the snapshotted files.