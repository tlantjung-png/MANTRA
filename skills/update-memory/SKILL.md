---
name: update-memory
description: Preserve verified facts that will matter in later sessions in the workspace memory ledger, without duplicating or retaining stale entries.
version: 1.2.0
user-invocable: true
---

# Update Memory

## Use When

Use after a durable decision, a verified fix, or a change in project state that
a later session needs to know. The console also records a short turn summary
automatically, so a routine completed turn needs no manual memory edit.

## Procedure

1. Read `<workspace>/.mantra/memory.md` and the current source of truth.
2. Write a concise dated entry that states the verified fact, its relevant
   path or test, and why it could affect later work. Follow the ledger's
   existing line format; do not replace it with a sectioned document.
3. If an active entry is now wrong, mark it superseded or replace it through
   the ledger's normal write path. Avoid near duplicates and temporary status
   notes that a saved session already preserves.
4. Re-read the ledger after writing and confirm the active entry reflects the
   current state.

## Verification

Confirm the fact against source or test output, and ensure the active-memory
tail still fits the prompt budget. A useful no-op is better than a duplicate.

## Boundaries

Do not store secrets, full transcripts, or unverified plans. Keep task-specific
checklists in the task report or a continuation brief rather than treating
memory as a live todo command.
