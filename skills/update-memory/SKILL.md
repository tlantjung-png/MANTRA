---
name: update-memory
description: Maintain a concise dated project-state ledger (workspace .mantra/memory.md) with goals, constraints, progress, decisions, next steps, critical context, and relevant files, pruning entries by future decision value.
version: 1.1.0
user-invocable: true
---

# Update Memory

## Use When

Use at meaningful checkpoints, after a task or fix batch, at a decision, or when
durable context should survive the session.

## Procedure

1. Read the existing project memory ledger (`<workspace>/.mantra/memory.md`) and
   preserve true history.
2. Keep exactly the sections Goal, Constraints, Progress, Key Decisions, Next
   Steps, Critical Context, and Relevant Files.
3. Check supersession when adding an entry: if the new fact supersedes an existing
   one, replace or consolidate the old entry in the same update rather than
   leaving both.
4. Record dated durable facts, verification commands, decisions, and blockers;
   remove stale current-state instructions.
5. Prune by future decision value: keep an entry when its rationale, boundary,
   security rule, or reintroduction condition could guide a future change; move
   closed one-off detail to a topic file or drop it. Length and age are discovery
   aids, never archive criteria.
6. Move detail to topic files when an entry grows beyond a couple of lines.
7. Cross-link the session goal and todo list without duplicating them: the
   standing goal (`/goal`) and checklist (`/todo`) are the operator's live state;
   memory records durable facts around them.

## Verification

Keep the file under 200 lines and 16,000 characters. Never silently truncate or
drop entries at the capacity ceiling; prune closed detail first. Prevent duplicate
entries and record only facts verified in the workspace or conversation.

## Boundaries

Do not store secrets, and do not add the ledger to version control unless
explicitly asked. Project state belongs here; style belongs in the repo's
conventions (see update-taste).